"""
chromadb_ingestor.py — Local vector database ingestion for the RAG chatbot.

After a bank statement is extracted and validated, every transaction is stored
in a local vector database called ChromaDB. This powers the RAG (Retrieval
Augmented Generation) chatbot that allows CID Karnataka investigators to ask
natural language questions like:
    "Show all transactions above ₹50,000"
    "Which account received money from ACC003 on 15th March?"
    "List all cash deposits in August 2022"

HOW VECTOR DATABASES WORK:
    A vector database converts text into numbers (called "embeddings") using
    a mathematical model. Similar pieces of text get similar numbers. When an
    investigator asks a question, the question is also converted to numbers,
    and the database finds the transactions with the most similar numbers.
    This is how the chatbot finds "relevant" transactions even when the exact
    words don't match.

LOCAL-ONLY ARCHITECTURE:
    ChromaDB runs entirely on the local disk at storage/chromadb/. No data
    is sent to any cloud service. The embedding model (all-MiniLM-L6-v2) is
    downloaded once and then runs entirely on the local CPU. This means:
      - All investigation data stays on the police machine
      - The chatbot works without internet after first setup
      - Multiple investigators can work independently without data mixing

ISOLATION BY SESSION:
    Each investigation session gets its own ChromaDB collection
    ("transactions_{session_id}"). This prevents different cases from
    interfering with each other when multiple investigators use the system
    simultaneously.

ONNXRUNTIME NOTE:
    On macOS, directly instantiating SentenceTransformer and calling .encode()
    can trigger a mutex stall in onnxruntime ("RAW: Lock blocking ...").
    We avoid this by using ChromaDB's built-in SentenceTransformerEmbeddingFunction,
    which registers the model on the collection. ChromaDB then calls the model
    internally during upsert() — a code path that does not hit the stall.

Team: Survey Corps | CIDECODE Hackathon 2026 | CID Karnataka
"""

import logging
import os
from typing import List, Tuple

import pandas as pd
import chromadb
from chromadb.utils import embedding_functions

# ── Prevent onnxruntime mutex stall on macOS ──────────────────────────────────
# SentenceTransformer uses onnxruntime internally. On macOS, onnxruntime's
# multi-threaded mode triggers a mutex deadlock ("RAW: Lock blocking ...").
# Setting these before the model loads forces single-threaded mode, which
# avoids the deadlock entirely. Must be set before any chromadb/ST import.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from config.settings import (
    CHROMADB_DIR,
    CHROMADB_COLLECTION_NAME,
    EMBEDDING_MODEL,
)

# Set up a logger for this module.
logger = logging.getLogger(__name__)

# Batch size for adding documents to ChromaDB.
# We add 100 at a time to avoid running out of memory on large case files.
BATCH_SIZE = 100


def ingest_transactions_to_chromadb(
    clean_df: pd.DataFrame,
    session_id: str,
) -> None:
    """
    Converts each transaction row into a descriptive sentence and
    stores it as a searchable vector embedding in ChromaDB.

    ChromaDB runs on local disk at storage/chromadb/.
    It is a local vector database — no data is sent to any cloud service.

    Each transaction is converted to a sentence like:
    "On 01/04/2024, account ACC001 (SBI) received a credit of
     Rs 5000.00 via UPI from Ramesh Kumar. Balance after transaction: Rs 45000.00"

    These sentences are embedded using the all-MiniLM-L6-v2 model
    (runs locally, no internet needed after first download) via ChromaDB's
    built-in embedding function to avoid the macOS onnxruntime mutex stall.

    A separate ChromaDB collection is created per session_id so that
    multiple concurrent investigations do not interfere with each other.

    Parameters:
        clean_df (pd.DataFrame): Clean validated transaction DataFrame
                                 from validator.validate_and_clean().
        session_id (str): Unique identifier for this investigation session.
                          Used to create a separate ChromaDB collection.

    Returns:
        None
    """
    if clean_df is None or clean_df.empty:
        logger.warning(
            "chromadb_ingestor.ingest_transactions_to_chromadb: "
            "Empty DataFrame received. Nothing to ingest."
        )
        return

    logger.info(
        "chromadb_ingestor.ingest_transactions_to_chromadb: "
        "Starting ingestion of %d transactions for session '%s'.",
        len(clean_df),
        session_id,
    )

    try:
        # ── Connect to ChromaDB on local disk ──────────────────────────────
        # PersistentClient stores everything on disk so data survives
        # after the program closes. The path is set in settings.py.
        chroma_client = chromadb.PersistentClient(path=str(CHROMADB_DIR))

        # ── Build collection name ──────────────────────────────────────────
        # Collection name format: "transactions_{session_id}"
        # e.g., "transactions_CASE_A_2024_06_19"
        # ChromaDB collection names must be 3-63 characters, alphanumeric + hyphens.
        collection_name = f"{CHROMADB_COLLECTION_NAME}_{session_id}"
        collection_name = collection_name[:63].replace(".", "-").replace(" ", "-")

        # ── Register ChromaDB's built-in SentenceTransformer embedding function ──
        # This avoids the onnxruntime mutex stall on macOS.
        # ChromaDB loads the model internally and calls it during upsert(),
        # bypassing the code path that triggers the stall.
        # WHAT IS SENT: only the transaction sentences (no PII, no raw bank data).
        # The model runs locally — no internet connection is used.
        st_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDING_MODEL,
        )

        collection = chroma_client.get_or_create_collection(
            name=collection_name,
            embedding_function=st_ef,
            metadata={"description": f"Transactions for investigation session {session_id}"},
        )

        logger.info(
            "chromadb_ingestor.ingest_transactions_to_chromadb: "
            "Using ChromaDB collection: '%s'",
            collection_name,
        )

        # ── Convert transactions to descriptive sentences ──────────────────
        # We only build the text documents here; ChromaDB uses the registered
        # SentenceTransformerEmbeddingFunction to compute embeddings automatically.
        documents, metadatas, doc_ids = _prepare_documents_text_only(
            clean_df, session_id
        )

        if not documents:
            logger.warning(
                "chromadb_ingestor.ingest_transactions_to_chromadb: "
                "No documents generated from DataFrame."
            )
            return

        # ── Add documents to ChromaDB in batches ──────────────────────────
        total_docs = len(documents)
        batches_added = 0

        for batch_start in range(0, total_docs, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_docs)

            batch_docs = documents[batch_start:batch_end]
            batch_metadatas = metadatas[batch_start:batch_end]
            batch_ids = doc_ids[batch_start:batch_end]

            # ChromaDB automatically computes embeddings using the registered
            # embedding_function (SentenceTransformerEmbeddingFunction).
            # upsert means "insert if not exists, update if exists" —
            # safe to run multiple times without creating duplicates.
            collection.upsert(
                documents=batch_docs,
                metadatas=batch_metadatas,
                ids=batch_ids,
            )

            batches_added += 1
            logger.info(
                "chromadb_ingestor.ingest_transactions_to_chromadb: "
                "Ingested batch %d: rows %d to %d of %d total.",
                batches_added,
                batch_start + 1,
                batch_end,
                total_docs,
            )

        logger.info(
            "chromadb_ingestor.ingest_transactions_to_chromadb: "
            "ChromaDB ingestion complete: %d vectors stored in collection '%s'.",
            total_docs,
            collection_name,
        )

    except Exception as error:
        logger.error(
            "chromadb_ingestor.ingest_transactions_to_chromadb: "
            "ChromaDB ingestion failed: %s",
            error,
        )


def _prepare_documents_text_only(
    clean_df: pd.DataFrame,
    session_id: str,
) -> Tuple[List[str], List[dict], List[str]]:
    """
    Converts each transaction row into a searchable sentence with metadata.

    This function does NOT compute embeddings — ChromaDB computes them
    automatically via the SentenceTransformerEmbeddingFunction registered
    on the collection. This design avoids the macOS onnxruntime mutex stall
    that occurs when SentenceTransformer is called directly in certain
    subprocess contexts.

    Parameters:
        clean_df (pd.DataFrame): Clean transaction DataFrame.
        session_id (str): Session identifier for generating unique document IDs.

    Returns:
        Tuple of three lists:
            - documents (List[str]): One natural-language sentence per transaction.
            - metadatas (List[dict]): Structured metadata per transaction for filtering.
            - doc_ids (List[str]): Unique string IDs per document.
    """
    documents: List[str] = []
    metadatas: List[dict] = []
    doc_ids: List[str] = []

    for row_index, (_, row) in enumerate(clean_df.iterrows()):
        try:
            # Convert the transaction row to a descriptive sentence
            sentence = _transaction_to_sentence(row)
            documents.append(sentence)

            # Build structured metadata for ChromaDB filter queries
            date_str = (
                str(row["Date"].date()) if pd.notna(row["Date"]) else "unknown"
            )
            metadata = {
                "account_id": str(row.get("Account_ID", "")),
                "bank_name": str(row.get("Bank_Name", "")),
                "date": date_str,
                "debit": float(row.get("Debit", 0.0)) if pd.notna(row.get("Debit")) else 0.0,
                "credit": float(row.get("Credit", 0.0)) if pd.notna(row.get("Credit")) else 0.0,
                "balance": float(row.get("Balance", 0.0)) if pd.notna(row.get("Balance")) else 0.0,
                "narration": str(row.get("Narration", "")),
                "session_id": session_id,
            }
            metadatas.append(metadata)

            # Unique ID: session + account + row index
            doc_id = f"{session_id}_{row.get('Account_ID', 'unknown')}_{row_index}"
            doc_ids.append(doc_id)

        except Exception as row_error:
            logger.warning(
                "chromadb_ingestor._prepare_documents_text_only: "
                "Skipped row %d due to error: %s",
                row_index,
                row_error,
            )
            continue

    return documents, metadatas, doc_ids


def _transaction_to_sentence(row: pd.Series) -> str:
    """
    Converts a single transaction row into a natural language sentence
    that can be searched semantically by the RAG chatbot.

    Template:
        "On {date}, account {account_id} ({bank_name}) [sent/received]
         [a debit of/a credit of] Rs {amount} via {narration}.
         Balance after transaction: Rs {balance}"

    The direction (sent/received) and type (debit/credit) is determined
    from the Debit and Credit column values.

    Parameters:
        row (pd.Series): A single transaction row from the clean DataFrame.

    Returns:
        str: A complete descriptive sentence for this transaction.
    """
    # Extract values with safe defaults
    date_str = str(row["Date"].date()) if pd.notna(row["Date"]) else "unknown date"
    account_id = str(row.get("Account_ID", "unknown account"))
    bank_name = str(row.get("Bank_Name", "unknown bank"))
    narration = str(row.get("Narration", ""))
    debit = float(row.get("Debit", 0.0)) if pd.notna(row.get("Debit")) else 0.0
    credit = float(row.get("Credit", 0.0)) if pd.notna(row.get("Credit")) else 0.0
    balance = float(row.get("Balance", 0.0)) if pd.notna(row.get("Balance")) else 0.0

    # Determine transaction direction
    if debit > 0:
        direction = "sent"
        txn_type = "a debit"
        amount = debit
    elif credit > 0:
        direction = "received"
        txn_type = "a credit"
        amount = credit
    else:
        direction = "had"
        txn_type = "a transaction"
        amount = 0.0

    sentence = (
        f"On {date_str}, account {account_id} ({bank_name}) {direction} "
        f"{txn_type} of Rs {amount:.2f} via {narration}. "
        f"Balance after transaction: Rs {balance:.2f}"
    )

    return sentence
