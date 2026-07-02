#!/usr/bin/env python3
"""Generate synthetic bank-statement CSVs for analysis-phase testing.

The generator is deterministic and embeds 10 evidence-backed fraud-pattern
families requested in SYNTHETIC DATASET GENERATION INST.md.
"""

from __future__ import annotations

import csv
import random
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "synthetic_data"

CSV_COLUMNS = [
    "account_number",
    "account_holder",
    "ifsc_code",
    "Date",
    "Time",
    "Narration",
    "Transaction_ID",
    "Reference_Number",
    "Transaction_Reference",
    "Cheque_Number",
    "Debit",
    "Credit",
    "Balance",
    "Transaction_Type",
    "Bank_Name",
    "txn_id",
    "duplicate_of",
    "is_reversed",
]

GT_COLUMNS = [
    "csv_file",
    "txn_id",
    "account_number",
    "date",
    "amount",
    "pattern_type",
    "pattern_role",
    "is_fraud",
    "notes",
]

BANKS = {
    "State Bank of India": {"prefix": "SBIN", "acct_len": 11, "holder": "upper"},
    "HDFC Bank": {"prefix": "HDFC", "acct_len": 14, "holder": "title"},
    "Axis Bank": {"prefix": "UTIB", "acct_len": 14, "holder": "upper"},
    "ICICI Bank": {"prefix": "ICIC", "acct_len": 12, "holder": "title"},
    "IDFC FIRST Bank": {"prefix": "IDFB", "acct_len": 11, "holder": "upper"},
    "Bank of Baroda": {"prefix": "BARB", "acct_len": 14, "holder": "title"},
    "Kotak Mahindra Bank": {"prefix": "KKBK", "acct_len": 10, "holder": "upper"},
    "Punjab National Bank": {"prefix": "PUNB", "acct_len": 16, "holder": "upper"},
    "Bank of Maharashtra": {"prefix": "MAHB", "acct_len": 11, "holder": "title"},
    "Indian Bank": {"prefix": "IDIB", "acct_len": 12, "holder": "upper"},
    "Canara Bank": {"prefix": "CNRB", "acct_len": 13, "holder": "upper"},
    "Federal Bank": {"prefix": "FDRL", "acct_len": 14, "holder": "title"},
}

NAMES = [
    "Ravi Kumar Sharma", "Priya Venkatesh", "Mohammed Salim", "Lakshmi Devi K",
    "Arjun Suresh Nair", "Kavya Bose", "Anjali Sunita", "Vikram Saxena",
    "Harish Ram", "Radha Rekha Saxena", "Yash Dubey", "Aishwarya Mehta",
    "Nitin Aditya", "Pankaj Pillai", "Farah Khan", "Sanjay Shetty",
    "Deepa Borale", "Kiran Nair", "Rohan Kapoor", "Meera Patel",
    "Aarav Ghosh", "Bhavana Rao", "Tarun Pillai", "Isha Reddy",
    "Gaurav Kunal Ghosh", "Komal Kapoor", "Devanshu Malhotra",
    "Sachin Sethi", "Rekha Yadav", "Shiv Lal Bishnoi",
]

BUSINESSES = [
    "Apex Commerce Enterprise", "Rudraya Hotel", "Crystal Supply Enterprise",
    "S R K Enterprises", "Sugarify Agency", "Maa Enterprise",
    "Legend Prime", "Techrevive Solutions", "Bansilal Traders",
    "Northern Traders Company", "Shree Krishna Mobile", "Global Enterprise",
    "Carmen Trinity Venture", "RG Enterprise", "Hanuman Dico",
]

MERCHANTS = [
    ("BESCOM", "bescom.bill@upi"), ("AIRTEL", "airtel.recharge@airtel"),
    ("DMART", "dmart.pay@okaxis"), ("INDIANOIL", "fuelpay@ybl"),
    ("JIO", "jio.mobile@ibl"), ("BIGBASKET", "bbdaily@okhdfcbank"),
    ("PHARMACY", "medplus@ybl"), ("IRCTC", "irctc.pay@axisbank"),
]


@dataclass
class Event:
    account: "Account"
    txn_date: date
    narration: str
    debit: float = 0.0
    credit: float = 0.0
    time: str = ""
    transaction_id: str = ""
    reference_number: str = ""
    transaction_reference: str = ""
    cheque_number: str = ""
    duplicate_of: str = ""
    duplicate_of_event: "Event | None" = None
    is_reversed: bool = False
    pattern_type: str = "innocent"
    pattern_role: str = "innocent"
    is_fraud: bool = False
    notes: str = ""
    order: int = 0
    balance: float = 0.0
    txn_id: str = ""

    @property
    def amount(self) -> float:
        return self.credit if self.credit else self.debit

    @property
    def transaction_type(self) -> str:
        return "credit" if self.credit else "debit"


@dataclass
class Account:
    number: str
    holder: str
    bank: str
    ifsc: str
    role: str
    opening_balance: float
    events: list[Event] = field(default_factory=list)

    def add(self, ev: Event) -> Event:
        ev.order = len(self.events)
        self.events.append(ev)
        return ev


class SyntheticGenerator:
    def __init__(self) -> None:
        self.rng = random.Random(20260626)
        self.used_accounts: set[str] = set()
        self.used_refs: set[str] = set()
        self.case_summaries: list[dict] = []
        self.ground_truth_rows: list[dict] = []
        self.total_transactions = 0
        self.total_fraud = 0

    def run(self) -> None:
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        self.generate_all_cases()
        self.write_ground_truth()
        self.validate_all()
        self.write_summary()

    def generate_all_cases(self) -> None:
        specs = [
            ("pattern_01_round_trip", "rt_case", 3, self.case_round_trip),
            ("pattern_02_transit_layering", "tl_case", 3, self.case_transit_layering),
            ("pattern_03_accumulation", "ac_case", 3, self.case_accumulation),
            ("pattern_04_structuring", "st_case", 4, self.case_structuring),
            ("pattern_05_burst_activity", "ba_case", 3, self.case_burst_activity),
            ("pattern_06_duplicates", "dup_case", 3, self.case_duplicates),
            ("pattern_07_money_trail", "mt_case", 4, self.case_money_trail),
            ("pattern_08_aggregation", "agg_case", 3, self.case_aggregation),
            ("pattern_09_circular_flow", "cf_case", 3, self.case_circular_flow),
            ("pattern_10_combined", "combo_case", 4, self.case_combined),
        ]
        for folder, prefix, count, fn in specs:
            (OUT / folder).mkdir(parents=True, exist_ok=True)
            for i in range(1, count + 1):
                fn(folder, f"{prefix}_{i:02d}.csv", i)

    def make_account(self, bank: str, role: str, opening: float | None = None) -> Account:
        meta = BANKS[bank]
        while True:
            first = str(self.rng.randint(1, 9))
            number = first + "".join(str(self.rng.randint(0, 9)) for _ in range(meta["acct_len"] - 1))
            if number not in self.used_accounts:
                self.used_accounts.add(number)
                break
        name = self.rng.choice(NAMES)
        holder = name.upper() if meta["holder"] == "upper" else name.title()
        ifsc = f"{meta['prefix']}0{self.rng.randint(100000, 999999)}"
        opening_balance = opening if opening is not None else float(self.rng.randint(60000, 350000))
        return Account(number, holder, bank, ifsc, role, opening_balance)

    def make_accounts(self, roles: list[str]) -> list[Account]:
        banks = list(BANKS)
        self.rng.shuffle(banks)
        accounts = []
        for idx, role in enumerate(roles):
            opening = 18000.0 if "transit" in role else None
            if "source" in role or "structuring" in role:
                opening = 900000.0
            accounts.append(self.make_account(banks[idx % len(banks)], role, opening))
        return accounts

    def ref(self, prefix: str = "", digits: int = 12) -> str:
        while True:
            body = "".join(str(self.rng.randint(0, 9)) for _ in range(digits))
            ref = f"{prefix}{body}" if prefix else body
            if ref not in self.used_refs:
                self.used_refs.add(ref)
                return ref

    def utr(self, bank: str, d: date, kind: str = "N") -> str:
        prefix = BANKS[bank]["prefix"]
        return f"{prefix}{kind}{d.strftime('%y%m%d')}{self.ref('', 7)}"

    def upi(self, name: str | None = None) -> str:
        bases = ["ravi", "priya", "meera", "arjun", "gaurav", "isha", "pankaj", "tarun"]
        suffixes = ["@ybl", "@axl", "@upi", "@okhdfcbank", "@okaxis", "@ibl", "@paytm"]
        base = (name or self.rng.choice(bases)).lower().split()[0]
        if self.rng.random() < 0.35:
            base = f"{self.rng.randint(7000000000, 9999999999)}"
        return f"{base}{self.rng.randint(1, 99)}{self.rng.choice(suffixes)}"

    def short(self, text: str) -> str:
        return "".join(x for x in text.upper() if x.isalnum())[:14] or "BENEFICIARY"

    def transfer_narration(self, account: Account, counterparty: Account, method: str, direction: str, ref: str, d: date) -> str:
        cp_name = counterparty.holder
        cp_ifsc = counterparty.ifsc
        cp_acct = counterparty.number
        own_upi = f"{account.number}@{account.ifsc.lower()}.ifsc.npci"
        cp_upi = f"{cp_acct}@{cp_ifsc.lower()}.ifsc.npci"
        bank = account.bank
        drcr = "DR" if direction == "debit" else "CR"
        if method == "UPI":
            if bank == "State Bank of India":
                other = cp_upi if direction == "debit" else own_upi
                sender = own_upi if direction == "debit" else cp_upi
                return f"UPI/{ref}/FROM: {sender}/TO: {other}. NPCI/UPI"
            if bank == "Axis Bank":
                tag = "PAY" if direction == "debit" else "REC"
                return f"UPI:{tag}:{ref}/{cp_name[:18]:<18}/{counterparty.bank[:10]}"
            if bank == "ICICI Bank":
                return f"UPI/{drcr}/{ref}/{self.short(cp_name)[:4]}/{cp_ifsc[:4]}/**{cp_upi}/UPI//ICI{self.ref('', 10)}"
            if bank == "IDFC FIRST Bank":
                word = "Pay request to" if direction == "debit" else "Payment from PhonePe"
                return f"UPI/MOB/{ref}/{word} {cp_name}/{cp_upi}"
            if bank == "Bank of Baroda":
                return f"UPI/{ref}/{self.random_time()}/UPI/{cp_upi}/Payment"
            if bank == "Kotak Mahindra Bank":
                return f"UPI/{ref}/{drcr}/{self.short(cp_name)[:4]}/{cp_ifsc[:4]}/{cp_upi}/UPI | {'D' if direction == 'debit' else 'C'}"
            if bank == "Punjab National Bank":
                mode = "P2A" if direction == "debit" else "P2V"
                return f"UPI/{ref}/{mode}/{cp_upi}/{cp_name[:20]} CDCI CDCI"
            if bank == "HDFC Bank":
                action = "SENT USING PAYTM UPI" if direction == "debit" else "PAYMENT FROM PHONEPE"
                return f"UPI/{ref}/{action}/{cp_upi}"
            return f"UPI/{ref}/{drcr}/{self.short(cp_name)}/{cp_ifsc[:4]}/{cp_upi}/UPI"

        if method == "IMPS":
            if bank == "Axis Bank":
                return f"MMT/IMPS/{ref}/{cp_name}/{cp_ifsc}"
            if bank == "State Bank of India":
                return f"IMPS/P2A/{cp_ifsc[:3]}/{cp_acct}/{ref}/1/BULK IMPS"
            if bank == "HDFC Bank":
                return f"IMPS {ref} {'TO' if direction == 'debit' else 'FROM'} {cp_name[:22]} {cp_ifsc[:4]}"
            if bank == "IDFC FIRST Bank":
                return f"IMPS-OP{'M' if direction == 'debit' else 'I'}/{ref}/{cp_name}/{cp_ifsc}/{cp_acct[-4:]}/"
            if bank == "Bank of Baroda":
                return f"DIGITA-MUMBAI/ IMPS/P2A/{ref}/{cp_name}/{cp_ifsc}"
            if bank == "Punjab National Bank":
                return f"IMPS-IN/{ref}/{cp_acct[-10:]}/{cp_name[:18]} CDCI CDCI"
            return f"IMPS/P2A/{ref}/{cp_ifsc}/{cp_name}"

        utr = ref
        if bank == "Axis Bank":
            return f"MB NEFT/{utr}/{cp_name[:10]}/{cp_name}/"
        if bank == "State Bank of India":
            prefix = "NEFT O/W" if direction == "debit" else "NEFT CR"
            return f"{prefix}-{utr}- {cp_ifsc}-{cp_acct}-{cp_name}- NEFT - {utr[-9:]} - {cp_acct} - {cp_ifsc}"
        if bank == "HDFC Bank":
            return f"NEFT/{utr}/{cp_name}/{d.strftime('%d-%b-%Y')}/{cp_ifsc}/{cp_acct}"
        if bank == "ICICI Bank":
            return f"NEFT:{utr}:{cp_name}"
        if bank == "IDFC FIRST Bank":
            return f"NEFT/{utr}/{cp_name}/{cp_ifsc}"
        if bank == "Bank of Baroda":
            return f"DIGITA-MUMBAI/ NEFT-{utr}-{cp_name}"
        if bank == "Kotak Mahindra Bank":
            side = "O/W- (Dr)" if direction == "debit" else "INW- (Cr)"
            return f"NEFT {utr} NEFT{side} {cp_name} {cp_ifsc} {cp_acct}"
        if bank == "Punjab National Bank":
            return f"NEFT/{utr}/{cp_name[:24]} CDCI CDCI"
        if bank == "Bank of Maharashtra":
            return f"{cp_name} {utr} - 9008-NEFT/ RTGS CELL {cp_ifsc} {cp_acct[-16:]}"
        if bank == "Canara Bank":
            return f"NEFT CR-{cp_ifsc}-{cp_name}-{utr}"
        if bank == "Federal Bank":
            return f"FT NEFT/{utr}/{cp_name}/{cp_name} TRF {d.strftime('%d-%b-%y')}"
        if bank == "Indian Bank":
            return f"N/{utr}/{cp_ifsc}/{cp_name}"
        return f"NEFT/{utr}/{cp_name}/{cp_ifsc}/{cp_acct}"

    def random_time(self) -> str:
        return f"{self.rng.randint(8, 22):02d}:{self.rng.randint(0, 59):02d}:{self.rng.randint(0, 59):02d}"

    def normal_narration(self, account: Account, kind: str, d: date, amount: float) -> tuple[str, str, str]:
        ref = self.ref("", 12)
        if kind == "salary":
            employer = self.rng.choice(BUSINESSES)
            utr = self.utr(account.bank, d, "N")
            ext = self.external_account(employer)
            return self.transfer_narration(account, ext, "NEFT", "credit", utr, d), utr, utr
        if kind == "emi":
            loan_ref = self.ref("", 10)
            return f"NACH DR-{loan_ref}-{self.rng.choice(['HDFC BANK', 'BAJAJ FINANCE', 'ICICI BANK'])} EMI-{account.number[-6:]}", "", ""
        if kind == "atm":
            return self.atm_narration(account, d), "", ""
        if kind == "interest":
            return self.interest_narration(account, d), "", ""
        if kind == "charges":
            return self.charge_narration(account, d), "", ""
        merchant, handle = self.rng.choice(MERCHANTS)
        fake = self.external_account(merchant, bank=self.rng.choice(list(BANKS)))
        fake.number = handle
        narration = self.transfer_narration(account, fake, "UPI", "debit", ref, d)
        if merchant not in narration:
            narration = f"{narration}/{merchant}"
        return narration, "", ref

    def external_account(self, name: str, bank: str | None = None) -> Account:
        bank = bank or self.rng.choice(list(BANKS))
        meta = BANKS[bank]
        num = "".join(str(self.rng.randint(0, 9)) for _ in range(meta["acct_len"]))
        holder = name.upper() if meta["holder"] == "upper" else name.title()
        return Account(num, holder, bank, f"{meta['prefix']}0{self.rng.randint(100000,999999)}", "external", 0.0)

    def atm_narration(self, account: Account, d: date) -> str:
        city = self.rng.choice(["JODHPUR", "LUCKNOW", "MYSORE", "PUNE", "JAIPUR", "KOZHIKODE"])
        if account.bank == "State Bank of India":
            return f"ATM Wdl P1EN{city[:3]}10 +{city} {d.strftime('%d/%m/%y')} {self.random_time()[:5]}"
        if account.bank == "Axis Bank":
            return f"NFSCWD ATM 6N{city}/{self.ref('',12)}/{self.random_time()}/{city}"
        if account.bank == "IDFC FIRST Bank":
            return f"ATM-NFS/CASH WITHDRAWAL/{city} BRANCH ATM {city} IN/{self.ref('',12)}/SELF"
        if account.bank == "Punjab National Bank":
            return f"ATM WDR {self.rng.randint(1000,9999)} PNB \\PNB {city} \\ {city} CDCI CDCI"
        return f"CWDR/{self.ref('',12)}/{d.strftime('%d-%m-%Y')} {self.random_time()}/CMN"

    def interest_narration(self, account: Account, d: date) -> str:
        start = d - timedelta(days=89)
        if account.bank == "ICICI Bank":
            return "Credit Interest Capitalised"
        if account.bank == "State Bank of India":
            return "HALF YEAR INTEREST"
        return f"{account.number}:Int.Pd:{start.strftime('%d-%m-%Y')} to {d.strftime('%d-%m-%Y')}"

    def charge_narration(self, account: Account, d: date) -> str:
        if account.bank == "HDFC Bank":
            return f"MB: IB: DC FEE SIGNATURE {d.strftime('%b').upper()} {d.strftime('%y')}"
        if account.bank == "Axis Bank":
            return f"ECSRTNCHGS{d.strftime('%d%m%y')}_SR{self.ref('',9)}GST"
        if account.bank == "State Bank of India":
            return f"AMB CHRGS FOR {d.strftime('%b %Y').upper()}"
        return self.rng.choice(["GST", "SMS ALERT CHARGES NEW", "MONTHLY SMS CHARGES", "MAB CHARGES"])

    def add_normal_activity(self, account: Account, start: date, end: date, target: int, pattern_type: str) -> None:
        current = start
        # Monthly salary/interest-like credits.
        for month in range(6):
            d = start + timedelta(days=month * 30 + self.rng.randint(0, 3))
            narration, ref_no, tx_ref = self.normal_narration(account, "salary", d, 0.0)
            account.add(Event(account, d, narration, credit=float(self.rng.randint(35000, 85000)),
                              reference_number=ref_no, transaction_reference=tx_ref,
                              pattern_type="innocent" if account.role == "innocent" else pattern_type,
                              pattern_role="innocent" if account.role == "innocent" else "normal_context",
                              notes="Normal recurring salary/business credit."))
            if month in (2, 5):
                account.add(Event(account, d + timedelta(days=1), self.interest_narration(account, d), credit=float(self.rng.randint(20, 400)),
                                  pattern_type="innocent" if account.role == "innocent" else pattern_type,
                                  pattern_role="innocent" if account.role == "innocent" else "normal_context",
                                  notes="Normal bank interest credit."))
        # Regular debits.
        while len(account.events) < target:
            current += timedelta(days=self.rng.randint(1, 3))
            if current > end:
                current = start + timedelta(days=self.rng.randint(10, 170))
            pick = self.rng.random()
            if pick < 0.08:
                amount = float(self.rng.choice([5000, 8000, 10000, 15000]))
                narration, ref_no, tx_ref = self.normal_narration(account, "atm", current, amount)
            elif pick < 0.16:
                amount = float(self.rng.choice([7500, 11200, 15850, 21400]))
                narration, ref_no, tx_ref = self.normal_narration(account, "emi", current, amount)
            elif pick < 0.22:
                amount = float(self.rng.choice([9, 18, 29, 59, 118]))
                narration, ref_no, tx_ref = self.normal_narration(account, "charges", current, amount)
            else:
                amount = float(self.rng.randint(80, 4500))
                narration, ref_no, tx_ref = self.normal_narration(account, "upi", current, amount)
            account.add(Event(account, current, narration, debit=amount,
                              reference_number=ref_no, transaction_reference=tx_ref,
                              time=self.random_time() if self.rng.random() < 0.16 else "",
                              pattern_type="innocent" if account.role == "innocent" else pattern_type,
                              pattern_role="innocent" if account.role == "innocent" else "normal_context",
                              notes="Normal non-pattern transaction."))

    def add_transfer(self, src: Account, dst: Account, d: date, amount: float, method: str,
                     pattern_type: str, src_role: str, dst_role: str, fraud: bool = True,
                     dst_fraud: bool | None = None, notes: str = "") -> tuple[Event, Event]:
        if method in {"NEFT", "RTGS"}:
            ref = self.utr(src.bank, d, "R" if method == "RTGS" else "N")
            ref_no = ref
        else:
            ref = self.ref("", 12)
            ref_no = "" if method == "UPI" else ref
        debit_narr = self.transfer_narration(src, dst, method, "debit", ref, d)
        credit_narr = self.transfer_narration(dst, src, method, "credit", ref, d)
        ev1 = src.add(Event(src, d, debit_narr, debit=round(amount, 1), reference_number=ref_no,
                            transaction_reference=ref, pattern_type=pattern_type,
                            pattern_role=src_role, is_fraud=fraud, notes=notes))
        ev2 = dst.add(Event(dst, d, credit_narr, credit=round(amount, 1), reference_number=ref_no,
                            transaction_reference=ref, pattern_type=pattern_type,
                            pattern_role=dst_role, is_fraud=fraud if dst_fraud is None else dst_fraud,
                            notes=notes))
        return ev1, ev2

    def finalize_case(self, folder: str, filename: str, accounts: list[Account], pattern_name: str, extra_summary: dict) -> None:
        rows = []
        gt_rows = []
        for acc in accounts:
            acc.events.sort(key=lambda e: (e.txn_date, e.time, e.order))
            self.ensure_positive_balance(acc)
            balance = acc.opening_balance
            for seq, ev in enumerate(acc.events):
                balance = round(balance - ev.debit + ev.credit, 1)
                ev.balance = balance
                ev.txn_id = f"{acc.number}_{seq:06d}"
            for ev in acc.events:
                if ev.duplicate_of_event:
                    ev.duplicate_of = ev.duplicate_of_event.txn_id
            for ev in acc.events:
                rows.append(self.event_to_row(ev))
                gt_rows.append({
                    "csv_file": f"{folder}/{filename}",
                    "txn_id": ev.txn_id,
                    "account_number": ev.account.number,
                    "date": fmt_date(ev.txn_date),
                    "amount": f"{ev.amount:.1f}",
                    "pattern_type": ev.pattern_type,
                    "pattern_role": ev.pattern_role,
                    "is_fraud": str(bool(ev.is_fraud)),
                    "notes": ev.notes,
                })
        rows.sort(key=lambda r: (to_iso(r["Date"]), r["Time"], r["account_number"], r["txn_id"]))
        path = OUT / folder / filename
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        self.ground_truth_rows.extend(gt_rows)
        fraud_count = sum(1 for r in gt_rows if r["is_fraud"] == "True")
        self.total_transactions += len(rows)
        self.total_fraud += fraud_count
        self.case_summaries.append({
            "file": f"{folder}/{filename}",
            "pattern": pattern_name,
            "accounts": [{"account_number": a.number, "holder": a.holder, "bank": a.bank, "role": a.role} for a in accounts],
            "transaction_count": len(rows),
            "fraud_count": fraud_count,
            **extra_summary,
        })

    def ensure_positive_balance(self, acc: Account) -> None:
        balance = acc.opening_balance
        min_balance = balance
        for ev in acc.events:
            balance = balance - ev.debit + ev.credit
            min_balance = min(min_balance, balance)
        if min_balance < 0:
            acc.opening_balance += abs(min_balance) + 50000.0

    def event_to_row(self, ev: Event) -> dict:
        return {
            "account_number": ev.account.number,
            "account_holder": ev.account.holder,
            "ifsc_code": ev.account.ifsc,
            "Date": fmt_date(ev.txn_date),
            "Time": ev.time,
            "Narration": ev.narration,
            "Transaction_ID": ev.transaction_id,
            "Reference_Number": ev.reference_number,
            "Transaction_Reference": ev.transaction_reference,
            "Cheque_Number": ev.cheque_number,
            "Debit": f"{ev.debit:.1f}",
            "Credit": f"{ev.credit:.1f}",
            "Balance": f"{ev.balance:.1f}",
            "Transaction_Type": ev.transaction_type,
            "Bank_Name": ev.account.bank,
            "txn_id": ev.txn_id,
            "duplicate_of": ev.duplicate_of,
            "is_reversed": str(bool(ev.is_reversed)),
        }

    # Pattern cases
    def base_case(self, roles: list[str], pattern_type: str, start_offset: int) -> tuple[list[Account], date, date]:
        accounts = self.make_accounts(roles)
        start = date(2025, 1, 1) + timedelta(days=start_offset)
        end = start + timedelta(days=180)
        for acc in accounts:
            target = 80
            self.add_normal_activity(acc, start, end, target, pattern_type)
        return accounts, start, end

    def case_round_trip(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["fraud_source", "fraud_intermediary", "fraud_intermediary", "partial_recipient", "innocent"], "round_trip", idx * 7)
        a, b, c, partial, _inn = accounts
        cycles = 3 + idx % 3
        amounts = []
        for cycle in range(cycles):
            d1 = start + timedelta(days=35 + cycle * 25)
            amt = float(self.rng.randint(140000, 380000))
            amt2 = round(amt * self.rng.uniform(0.90, 0.97), 1)
            amt3 = round(amt * self.rng.uniform(0.85, 0.96), 1)
            self.add_transfer(a, b, d1, amt, "NEFT", "round_trip", "round_trip_sender", "round_trip_intermediary_hop1", notes=f"Round trip cycle {cycle+1}: A to B.")
            self.add_transfer(b, c, d1 + timedelta(days=self.rng.randint(2, 6)), amt2, "NEFT", "round_trip", "round_trip_intermediary_hop1", "round_trip_intermediary_hop2", notes=f"Round trip cycle {cycle+1}: B to C.")
            self.add_transfer(c, a, d1 + timedelta(days=self.rng.randint(7, 14)), amt3, "NEFT", "round_trip", "round_trip_intermediary_hop2", "round_trip_receiver", notes=f"Round trip cycle {cycle+1}: C returns funds to A.")
            amounts.append(amt)
        self.add_transfer(a, partial, start + timedelta(days=120), 42000.0, "UPI", "round_trip", "partial_connected_sender", "partial_connected_receiver", fraud=True, dst_fraud=False, notes="Partial account receives one payment but does not participate further.")
        self.finalize_case(folder, filename, accounts, "Round Trip", {"cycles": cycles, "amount_range": [min(amounts), max(amounts)]})

    def case_transit_layering(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["fraud_source", "transit_passthrough", "fraud_destination", "partial_recipient", "innocent"], "transit_layering", idx * 9)
        a, b, c, partial, _inn = accounts
        cycles = 6 + idx
        amounts = []
        for cycle in range(cycles):
            d = start + timedelta(days=30 + cycle * 16)
            amt = float(self.rng.randint(90000, 260000))
            out = round(amt * self.rng.uniform(0.90, 0.97), 1)
            self.add_transfer(a, b, d, amt, "RTGS" if cycle % 2 else "NEFT", "transit_layering", "transit_source", "transit_passthrough", notes=f"Transit cycle {cycle+1}: source funds transit account.")
            self.add_transfer(b, c, d + timedelta(days=self.rng.randint(1, 3)), out, "NEFT", "transit_layering", "transit_passthrough", "transit_destination", notes=f"Transit cycle {cycle+1}: passthrough exits 90-97 percent.")
            amounts.append(amt)
        self.add_transfer(b, partial, start + timedelta(days=150), 18000.0, "UPI", "transit_layering", "partial_connected_sender", "partial_connected_receiver", fraud=True, dst_fraud=False, notes="Partial connected small payment.")
        self.finalize_case(folder, filename, accounts, "Transit / Layering", {"cycles": cycles, "amount_range": [min(amounts), max(amounts)]})

    def case_accumulation(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["sender", "sender", "sender", "accumulation_account", "partial_sender", "innocent"], "accumulation", idx * 11)
        senders = accounts[:3]
        dacc = accounts[3]
        partial = accounts[4]
        deposits = 18 + idx * 3
        amounts = []
        for i in range(deposits):
            src = self.rng.choice(senders + [partial])
            d = start + timedelta(days=30 + i * 5)
            amt = float(self.rng.randint(18000, 95000))
            dst_fraud = src is not partial
            self.add_transfer(src, dacc, d, amt, self.rng.choice(["UPI", "NEFT", "IMPS"]), "accumulation", "accumulation_sender", "accumulation_deposit", fraud=dst_fraud, dst_fraud=True, notes=f"Accumulation deposit {i+1} into collector account.")
            amounts.append(amt)
        for i in range(3):
            d = start + timedelta(days=145 + i * 9)
            dacc.add(Event(dacc, d, self.atm_narration(dacc, d), debit=float(self.rng.randint(3000, 9000)), pattern_type="accumulation", pattern_role="small_withdrawal", is_fraud=False, notes="Small occasional withdrawal from accumulation account."))
        self.finalize_case(folder, filename, accounts, "Accumulation", {"cycles": deposits, "amount_range": [min(amounts), max(amounts)]})

    def case_structuring(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["structuring_sender", "partial_recipient", "partial_recipient", "partial_recipient", "innocent"], "structuring", idx * 5)
        src = accounts[0]
        recips = accounts[1:4]
        days = 3 + (idx % 3)
        amounts = []
        for day_idx in range(days):
            d = start + timedelta(days=42 + day_idx * 28)
            for j in range(4 + (day_idx % 3)):
                dst = recips[j % len(recips)]
                amt = float(self.rng.randint(85000, 99500))
                self.add_transfer(src, dst, d, amt, "NEFT" if j % 2 else "UPI", "structuring", "structuring_transfer", "structuring_receiver", fraud=True, dst_fraud=False, notes=f"Same-day below-threshold transfer set {day_idx+1}.")
                amounts.append(amt)
        self.finalize_case(folder, filename, accounts, "Structuring / Smurfing", {"cycles": days, "amount_range": [min(amounts), max(amounts)]})

    def case_burst_activity(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["burst_account", "fraud_counterparty", "fraud_counterparty", "partial_recipient", "innocent"], "burst_activity", idx * 13)
        e = accounts[0]
        cps = accounts[1:4]
        burst_start = start + timedelta(days=102)
        burst_count = 85 + idx * 12
        for i in range(burst_count):
            d = burst_start + timedelta(days=i % 11)
            cp = cps[i % len(cps)]
            amt = float(self.rng.randint(1200, 42000))
            if i % 3 == 0:
                self.add_transfer(cp, e, d, amt, "UPI", "burst_activity", "burst_counterparty", "burst_active_period", fraud=True, dst_fraud=True, notes="Inbound transaction during 11-day burst window.")
            else:
                self.add_transfer(e, cp, d, amt, "UPI" if i % 2 else "NEFT", "burst_activity", "burst_active_period", "burst_counterparty", fraud=True, dst_fraud=(cp.role != "partial_recipient"), notes="Outbound transaction during 11-day burst window.")
        self.finalize_case(folder, filename, accounts, "Burst Activity", {"cycles": burst_count, "burst_window": [fmt_date(burst_start), fmt_date(burst_start + timedelta(days=10))]})

    def case_duplicates(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["duplicate_account", "counterparty", "counterparty", "partial_recipient", "innocent"], "duplicate", idx * 10)
        a, b, c, partial, _ = accounts
        pairs = 5 + idx
        originals = []
        for i in range(pairs):
            d = start + timedelta(days=45 + i * 14)
            dst = b if i % 2 else c
            orig, recv = self.add_transfer(a, dst, d, float(self.rng.randint(3000, 32000)), "UPI", "duplicate", "duplicate_original", "duplicate_counterparty", fraud=True, notes=f"Original transaction for duplicate pair {i+1}.")
            if i % 2 == 0:
                dup_narr = orig.narration
                dup_ref = orig.transaction_reference
            else:
                # Near-duplicate: identical except for a fresh reference number, kept
                # consistent between the narration string and Transaction_Reference.
                dup_ref = self.ref("", 12)
                dup_narr = orig.narration.replace(orig.transaction_reference, dup_ref)
            dup = a.add(Event(a, d, dup_narr, debit=orig.debit,
                              transaction_reference=dup_ref,
                              duplicate_of_event=orig, pattern_type="duplicate", pattern_role="duplicate_copy", is_fraud=True,
                              notes=f"Duplicate copy of {i+1}; duplicate_of points to original txn_id."))
            if i % 3 == 0:
                rev_ref = orig.transaction_reference
                a.add(Event(a, d + timedelta(days=1), f"REVERSAL OF UPI/{rev_ref}/{dst.holder}/{dst.number}", credit=orig.debit,
                            transaction_reference=rev_ref, is_reversed=True, pattern_type="duplicate", pattern_role="reversal_credit", is_fraud=True,
                            notes="Reversal credit for original debit."))
            originals.append(orig)
        self.add_transfer(a, partial, start + timedelta(days=160), 2500.0, "UPI", "duplicate", "partial_connected_sender", "partial_connected_receiver", fraud=True, dst_fraud=False, notes="Partial connected recipient.")
        self.finalize_case(folder, filename, accounts, "Duplicate Transactions", {"cycles": pairs, "key_transactions": len(originals)})

    def case_money_trail(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["money_trail_origin", "trail_recipient", "trail_recipient", "trail_recipient", "partial_recipient", "innocent"], "money_trail", idx * 12)
        trails = 3 if idx == 3 else (2 if idx == 4 else 1)
        for trail in range(trails):
            f = accounts[trail % 3]
            g, h, iacc = accounts[1], accounts[2], accounts[3]
            d = start + timedelta(days=55 + trail * 35)
            source = self.external_account("Actuate Solution Private", bank="HDFC Bank")
            amount = float(500000 + trail * 75000)
            self.add_transfer(source, f, d, amount, "NEFT", "money_trail", "external_source", "money_trail_source_credit", fraud=True, dst_fraud=True, notes=f"Large source credit starts money trail {trail+1}.")
            self.add_transfer(f, g, d + timedelta(days=2), 150000.0 + trail * 20000, "NEFT", "money_trail", "money_trail_diversion", "trail_receiver", notes=f"Money trail {trail+1} diversion to recipient G.")
            self.add_transfer(f, h, d + timedelta(days=4), 200000.0 + trail * 15000, "NEFT", "money_trail", "money_trail_diversion", "trail_receiver", notes=f"Money trail {trail+1} diversion to recipient H.")
            self.add_transfer(f, iacc, d + timedelta(days=7), 80000.0 + trail * 10000, "UPI", "money_trail", "money_trail_diversion", "trail_receiver", notes=f"Money trail {trail+1} UPI diversion.")
            f.add(Event(f, d + timedelta(days=9), self.atm_narration(f, d), debit=50000.0, pattern_type="money_trail", pattern_role="money_trail_diversion", is_fraud=True, notes=f"Money trail {trail+1} cash withdrawal."))
        self.finalize_case(folder, filename, accounts, "Money Trail", {"cycles": trails})

    def case_aggregation(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["aggregation_collector", "small_sender", "small_sender", "aggregation_exit", "partial_sender", "innocent"], "aggregation", idx * 6)
        collector = accounts[0]
        senders = accounts[1:3] + [accounts[4]]
        exit_acc = accounts[3]
        rounds = 2 + (idx % 2)
        total_collected = []
        for r in range(rounds):
            base = start + timedelta(days=35 + r * 48)
            round_total = 0.0
            for i in range(22 + idx * 3):
                src = self.rng.choice(senders)
                amt = float(self.rng.randint(500, 5000))
                round_total += amt
                self.add_transfer(src, collector, base + timedelta(days=i % 18), amt, "UPI", "aggregation", "aggregation_small_sender", "aggregation_collection", fraud=(src.role != "partial_sender"), dst_fraud=True, notes=f"Small aggregation credit round {r+1}.")
            self.add_transfer(collector, exit_acc, base + timedelta(days=21), round(round_total * 0.92, 1), "NEFT", "aggregation", "aggregation_transfer", "aggregation_exit_account", notes=f"Large transfer after aggregation round {r+1}.")
            total_collected.append(round_total)
        self.finalize_case(folder, filename, accounts, "Aggregation", {"cycles": rounds, "amount_range": [min(total_collected), max(total_collected)]})

    def case_circular_flow(self, folder: str, filename: str, idx: int) -> None:
        accounts, start, _ = self.base_case(["circular_a", "circular_b", "circular_c", "circular_d", "partial_recipient", "innocent"], "circular_flow", idx * 8)
        a, b, c, dacc, partial, _ = accounts
        cycles = 3 + (idx % 2)
        amounts = []
        for cyc in range(cycles):
            d = start + timedelta(days=40 + cyc * 33)
            amt = float(self.rng.randint(180000, 480000))
            self.add_transfer(a, b, d, amt, "NEFT", "circular_flow", "circular_hop1", "circular_hop1_receiver", notes=f"Circular cycle {cyc+1}: A to B.")
            self.add_transfer(b, c, d + timedelta(days=2), round(amt * 0.95, 1), "NEFT", "circular_flow", "circular_hop2", "circular_hop2_receiver", notes=f"Circular cycle {cyc+1}: B to C.")
            if idx == 2:
                self.add_transfer(c, dacc, d + timedelta(days=4), round(amt * 0.92, 1), "NEFT", "circular_flow", "circular_hop3", "circular_hop3_receiver", notes=f"Circular cycle {cyc+1}: C to D.")
                self.add_transfer(dacc, a, d + timedelta(days=6), round(amt * 0.89, 1), "NEFT", "circular_flow", "circular_hop4", "circular_receiver", notes=f"Circular cycle {cyc+1}: D returns to A.")
            else:
                self.add_transfer(c, a, d + timedelta(days=6), round(amt * 0.90, 1), "NEFT", "circular_flow", "circular_hop3", "circular_receiver", notes=f"Circular cycle {cyc+1}: C returns to A.")
            amounts.append(amt)
        self.add_transfer(a, partial, start + timedelta(days=150), 33000.0, "UPI", "circular_flow", "partial_connected_sender", "partial_connected_receiver", fraud=True, dst_fraud=False, notes="Partial connected payment outside cycle.")
        self.finalize_case(folder, filename, accounts, "Circular Flow", {"cycles": cycles, "amount_range": [min(amounts), max(amounts)]})

    def case_combined(self, folder: str, filename: str, idx: int) -> None:
        if idx == 1:
            accounts, start, _ = self.base_case(["fraud_source", "fraud_intermediary", "fraud_intermediary", "structuring_sender", "partial_recipient", "innocent"], "combined", 20)
            a, b, c, s, p, _ = accounts
            for cyc in range(3):
                d = start + timedelta(days=38 + cyc * 28)
                amt = float(self.rng.randint(160000, 320000))
                self.add_transfer(a, b, d, amt, "NEFT", "combined", "round_trip_sender", "round_trip_intermediary_hop1", notes="Combined file: round trip leg.")
                self.add_transfer(b, c, d + timedelta(days=4), round(amt * 0.93, 1), "NEFT", "combined", "round_trip_intermediary_hop1", "round_trip_intermediary_hop2", notes="Combined file: round trip leg.")
                self.add_transfer(c, a, d + timedelta(days=9), round(amt * 0.90, 1), "NEFT", "combined", "round_trip_intermediary_hop2", "round_trip_receiver", notes="Combined file: round trip return.")
            for j in range(18):
                self.add_transfer(s, p, start + timedelta(days=75 + (j // 6) * 20), float(self.rng.randint(85000, 99500)), "UPI", "combined", "structuring_transfer", "structuring_receiver", fraud=True, dst_fraud=False, notes="Combined file: below-threshold structuring transfer.")
        elif idx == 2:
            accounts, start, _ = self.base_case(["fraud_source", "transit_passthrough", "accumulation_account", "burst_account", "partial_recipient", "innocent"], "combined", 40)
            src, transit, collector, burst, partial, _ = accounts
            for cyc in range(7):
                d = start + timedelta(days=28 + cyc * 15)
                amt = float(self.rng.randint(70000, 210000))
                self.add_transfer(src, transit, d, amt, "RTGS", "combined", "transit_source", "transit_passthrough", notes="Combined transit funding.")
                self.add_transfer(transit, collector, d + timedelta(days=2), round(amt * 0.94, 1), "NEFT", "combined", "transit_passthrough", "accumulation_deposit", notes="Combined transit exits into accumulation account.")
            for i in range(90):
                cp = self.rng.choice([src, transit, collector, partial])
                if i % 2:
                    self.add_transfer(burst, cp, start + timedelta(days=102 + i % 11), float(self.rng.randint(1000, 35000)), "UPI", "combined", "burst_active_period", "burst_counterparty", fraud=True, dst_fraud=(cp is not partial), notes="Combined burst activity.")
                else:
                    self.add_transfer(cp, burst, start + timedelta(days=102 + i % 11), float(self.rng.randint(1000, 35000)), "UPI", "combined", "burst_counterparty", "burst_active_period", fraud=(cp is not partial), dst_fraud=True, notes="Combined burst activity.")
        elif idx == 3:
            accounts, start, _ = self.base_case(["money_trail_origin", "trail_recipient", "trail_recipient", "aggregation_collector", "partial_sender", "innocent"], "combined", 60)
            f, g, h, collector, partial, _ = accounts
            source = self.external_account("Ronak Floor Mills Private", bank="HDFC Bank")
            self.add_transfer(source, f, start + timedelta(days=60), 575000.0, "NEFT", "combined", "money_trail_source", "money_trail_source_credit", notes="Combined money trail source credit.")
            self.add_transfer(f, g, start + timedelta(days=62), 180000.0, "NEFT", "combined", "money_trail_diversion", "trail_receiver", notes="Combined money trail diversion.")
            self.add_transfer(f, h, start + timedelta(days=64), 210000.0, "NEFT", "combined", "money_trail_diversion", "trail_receiver", notes="Combined money trail diversion.")
            total = 0.0
            for i in range(32):
                src = self.rng.choice([g, h, partial])
                amt = float(self.rng.randint(500, 5000))
                total += amt
                self.add_transfer(src, collector, start + timedelta(days=80 + i % 18), amt, "UPI", "combined", "aggregation_small_sender", "aggregation_collection", fraud=(src is not partial), dst_fraud=True, notes="Combined aggregation credit.")
            self.add_transfer(collector, f, start + timedelta(days=102), round(total * 0.9, 1), "NEFT", "combined", "aggregation_transfer", "aggregation_exit_account", notes="Combined aggregation exit transfer.")
        else:
            accounts, start, _ = self.base_case(["circular_a", "circular_b", "circular_c", "duplicate_account", "partial_recipient", "innocent"], "combined", 80)
            a, b, c, dup, partial, _ = accounts
            for cyc in range(3):
                d = start + timedelta(days=35 + cyc * 30)
                amt = float(self.rng.randint(170000, 360000))
                self.add_transfer(a, b, d, amt, "NEFT", "combined", "circular_hop1", "circular_hop1_receiver", notes="Combined circular hop.")
                self.add_transfer(b, c, d + timedelta(days=2), round(amt * 0.95, 1), "NEFT", "combined", "circular_hop2", "circular_hop2_receiver", notes="Combined circular hop.")
                self.add_transfer(c, a, d + timedelta(days=6), round(amt * 0.9, 1), "NEFT", "combined", "circular_hop3", "circular_receiver", notes="Combined circular return.")
            for i in range(6):
                orig, _recv = self.add_transfer(dup, partial, start + timedelta(days=70 + i * 9), float(self.rng.randint(2500, 18000)), "UPI", "combined", "duplicate_original", "duplicate_counterparty", fraud=True, dst_fraud=False, notes="Combined duplicate original.")
                dup.add(Event(dup, orig.txn_date, orig.narration, debit=orig.debit, transaction_reference=orig.transaction_reference, duplicate_of_event=orig, pattern_type="combined", pattern_role="duplicate_copy", is_fraud=True, notes="Combined duplicate copy."))
        self.finalize_case(folder, filename, accounts, "Combined Patterns", {"cycles": "combined"})

    def write_ground_truth(self) -> None:
        gt_dir = OUT / "ground_truth"
        gt_dir.mkdir(parents=True, exist_ok=True)
        with (gt_dir / "GROUND_TRUTH.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=GT_COLUMNS)
            writer.writeheader()
            writer.writerows(self.ground_truth_rows)

    def validate_all(self) -> None:
        errors: list[str] = []
        files = sorted(p for p in OUT.glob("pattern_*/*.csv"))
        if len(files) != 33:
            errors.append(f"Expected 33 CSV files, found {len(files)}")
        fraud_gt = sum(1 for r in self.ground_truth_rows if r["is_fraud"] == "True")
        if fraud_gt != self.total_fraud:
            errors.append("Fraud count mismatch between generated rows and ground truth")
        for path in files:
            with path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows and list(rows[0].keys()) != CSV_COLUMNS:
                errors.append(f"{path}: schema mismatch")
            if len(rows) < 200:
                errors.append(f"{path}: fewer than 200 transactions")
            if "pattern_10_combined" in str(path) and len(rows) < 300:
                errors.append(f"{path}: combined file fewer than 300 transactions")
            by_acc: dict[str, list[dict]] = defaultdict(list)
            for row in rows:
                by_acc[row["account_number"]].append(row)
                ref = row["Transaction_Reference"]
                if ref and ref not in row["Narration"]:
                    errors.append(f"{path}: ref {ref} missing in narration for {row['txn_id']}")
                debit = float(row["Debit"])
                credit = float(row["Credit"])
                if (debit > 0 and credit > 0) or (debit == 0 and credit == 0):
                    errors.append(f"{path}: debit/credit invalid for {row['txn_id']}")
                if row["Transaction_Type"] not in {"debit", "credit"}:
                    errors.append(f"{path}: invalid Transaction_Type")
            for acc, acc_rows in by_acc.items():
                if len(acc_rows) < 80:
                    errors.append(f"{path}: account {acc} has fewer than 80 transactions")
                acc_rows.sort(key=lambda r: (to_iso(r["Date"]), r["Time"], r["txn_id"]))
                # Infer opening from first row and then walk the chain.
                first = acc_rows[0]
                running = round(float(first["Balance"]) + float(first["Debit"]) - float(first["Credit"]), 1)
                for row in acc_rows:
                    running = round(running - float(row["Debit"]) + float(row["Credit"]), 1)
                    if abs(running - float(row["Balance"])) > 0.01:
                        errors.append(f"{path}: balance break at {row['txn_id']}")
                    if running < -0.01:
                        errors.append(f"{path}: negative balance at {row['txn_id']}")
            refs = defaultdict(list)
            for row in rows:
                if row["Reference_Number"]:
                    refs[row["Reference_Number"]].append(row)
            for ref, ref_rows in refs.items():
                if len(ref_rows) >= 2:
                    vals = {round(max(float(r["Debit"]), float(r["Credit"])), 1) for r in ref_rows}
                    if len(vals) > 1:
                        errors.append(f"{path}: linked UTR {ref} amount mismatch")
            role_by_acc = {a["account_number"]: a["role"] for cs in self.case_summaries if cs["file"] == rel_synth(path) for a in cs["accounts"]}
            fraud_tokens = [a["account_number"] for a in self.case_summaries_by_file(rel_synth(path))["accounts"] if a["role"] != "innocent"]
            fraud_names = [a["holder"] for a in self.case_summaries_by_file(rel_synth(path))["accounts"] if a["role"] != "innocent"]
            for row in rows:
                if role_by_acc.get(row["account_number"]) == "innocent":
                    text = row["Narration"]
                    for token in fraud_tokens + fraud_names:
                        if token and token in text:
                            errors.append(f"{path}: innocent transaction references fraud entity {token}")
        if errors:
            raise SystemExit("Quality check failed:\n" + "\n".join(errors[:80]))

    def case_summaries_by_file(self, csv_file: str) -> dict:
        for cs in self.case_summaries:
            if cs["file"] == csv_file:
                return cs
        raise KeyError(csv_file)

    def write_summary(self) -> None:
        innocent = len(self.ground_truth_rows) - self.total_fraud
        lines = [
            "# Ground Truth Summary",
            "",
            "## Total Statistics",
            f"- Total CSV files generated: 33",
            f"- Total transactions (all files): {len(self.ground_truth_rows)}",
            f"- Total fraud transactions: {self.total_fraud}",
            f"- Total innocent transactions: {innocent}",
            f"- Fraud percentage: {self.total_fraud / max(1, len(self.ground_truth_rows)) * 100:.2f}%",
            "",
            "## Per-Pattern Summary",
        ]
        grouped = defaultdict(list)
        for cs in self.case_summaries:
            grouped[cs["pattern"]].append(cs)
        for pattern, cases in grouped.items():
            fraud_accounts = sorted({a["account_number"] for cs in cases for a in cs["accounts"] if a["role"] != "innocent" and "partial" not in a["role"]})
            innocent_accounts = sorted({a["account_number"] for cs in cases for a in cs["accounts"] if a["role"] == "innocent"})
            lines.extend([
                "",
                f"### {pattern}",
                "- Files: " + ", ".join(Path(cs["file"]).name for cs in cases),
                "- Accounts involved in fraud: " + ", ".join(fraud_accounts[:20]),
                "- Innocent accounts: " + ", ".join(innocent_accounts[:20]),
                f"- Total pattern transactions flagged as fraud: {sum(cs['fraud_count'] for cs in cases)}",
                "- Detection signal: matched references, linked counterparties in narration, amounts, timing windows, and balance behaviour.",
            ])
        lines.extend(["", "## Per-File Summary"])
        for cs in self.case_summaries:
            key_txns = [r["txn_id"] for r in self.ground_truth_rows if r["csv_file"] == cs["file"] and r["is_fraud"] == "True"][:20]
            lines.extend([
                "",
                f"### {Path(cs['file']).name}",
                "- Relative path: " + cs["file"],
                "- Pattern: " + cs["pattern"],
                f"- Transactions: {cs['transaction_count']}",
                f"- Fraud transactions: {cs['fraud_count']}",
                "- Accounts: " + ", ".join(f"{a['account_number']} ({a['role']})" for a in cs["accounts"]),
                "- Key transactions to detect: " + ", ".join(key_txns),
            ])
        (OUT / "ground_truth" / "GROUND_TRUTH_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def to_iso(s: str) -> str:
    return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")


def rel_synth(path: Path) -> str:
    return str(path.relative_to(OUT))


if __name__ == "__main__":
    SyntheticGenerator().run()
    print(f"Generated synthetic dataset under {OUT}")
