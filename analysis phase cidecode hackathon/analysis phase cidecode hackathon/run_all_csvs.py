"""
run_all_csvs.py — Native Desktop GUI Assistant for Bank Statement Analysis
Supports native Windows Drag & Drop for statement CSV files.
"""
import os
import sys
import shutil
import subprocess
import threading
import queue
from pathlib import Path

# Tkinter imports
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Native Windows Drag & Drop library
import windnd

PROJECT_ROOT = Path(__file__).parent.resolve()
OUTPUTS = PROJECT_ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)
ANALYSIS_OP = PROJECT_ROOT / "analysis_op"

class AnalysisGUIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CiDeCode — Bank Statement Analysis Assistant")
        self.root.geometry("700x550")
        self.root.configure(bg="#05080f")
        self.root.resizable(False, False)
        
        self.log_queue = queue.Queue()
        self.is_running = False
        
        self.setup_styles()
        self.create_widgets()
        
        # Initialize Windows Drag & Drop hook
        self.root.update()
        windnd.hook_dropfiles(self.root, func=self.handle_drop)
        
    def setup_styles(self):
        # Configure fonts and general ttk styling
        self.title_font = ("Helvetica", 16, "bold")
        self.normal_font = ("Helvetica", 10)
        self.mono_font = ("Consolas", 9)
        self.bold_font = ("Helvetica", 10, "bold")
        
    def create_widgets(self):
        # ── Header ──────────────────────────────────────────────────────────
        header_frame = tk.Frame(self.root, bg="#05080f", pady=15)
        header_frame.pack(fill="x")
        
        title_label = tk.Label(
            header_frame, 
            text="CiDeCode Financial Crime Analysis", 
            font=self.title_font, 
            bg="#05080f", 
            fg="#22d3ee"
        )
        title_label.pack()
        
        sub_label = tk.Label(
            header_frame, 
            text="Drag & drop any transaction CSV file below to run fraud patterns", 
            font=self.normal_font, 
            bg="#05080f", 
            fg="#94a3b8"
        )
        sub_label.pack(pady=2)

        # ── Drop Zone area ──────────────────────────────────────────────────
        self.drop_frame = tk.Frame(
            self.root, 
            bg="#0f172a", 
            highlightbackground="#1e293b", 
            highlightthickness=1, 
            bd=0
        )
        self.drop_frame.pack(fill="x", padx=30, pady=10)
        
        self.drop_label = tk.Label(
            self.drop_frame, 
            text="📂\nDrag & Drop CSV File Here\n(or click to browse)", 
            font=("Helvetica", 12, "bold"), 
            bg="#0f172a", 
            fg="#a78bfa", 
            cursor="hand2", 
            pady=30
        )
        self.drop_label.pack(fill="both", expand=True)
        
        # Bind click to open file browser
        self.drop_label.bind("<Button-1>", lambda e: self.browse_file())
        
        # Highlight on hover
        self.drop_label.bind("<Enter>", lambda e: self.drop_frame.configure(highlightbackground="#22d3ee"))
        self.drop_label.bind("<Leave>", lambda e: self.drop_frame.configure(highlightbackground="#1e293b"))

        # ── Log / Console display area ──────────────────────────────────────
        console_label = tk.Label(
            self.root, 
            text="Pipeline Logs", 
            font=self.bold_font, 
            bg="#05080f", 
            fg="#e2e8f0"
        )
        console_label.pack(anchor="w", padx=30, pady=(15, 2))
        
        console_frame = tk.Frame(self.root, bg="#020617")
        console_frame.pack(fill="both", expand=True, padx=30, pady=(0, 20))
        
        self.scrollbar = tk.Scrollbar(console_frame)
        self.scrollbar.pack(side="right", fill="y")
        
        self.log_text = tk.Text(
            console_frame, 
            font=self.mono_font, 
            bg="#020617", 
            fg="#94a3b8", 
            yscrollcommand=self.scrollbar.set,
            bd=0,
            padx=10,
            pady=10
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        self.scrollbar.config(command=self.log_text.yview)
        
        self.log_text.insert("1.0", "System ready. Drop a CSV file to begin analysis.\n")
        self.log_text.config(state="disabled")

        # ── Footer / Status ─────────────────────────────────────────────────
        self.footer_frame = tk.Frame(self.root, bg="#05080f", pady=10)
        self.footer_frame.pack(fill="x", side="bottom")
        
        self.status_label = tk.Label(
            self.footer_frame, 
            text="Status: Idle", 
            font=self.bold_font, 
            bg="#05080f", 
            fg="#64748b"
        )
        self.status_label.pack(side="left", padx=30)
        
        self.action_btn = tk.Button(
            self.footer_frame,
            text="Open Outputs Directory",
            command=self.open_outputs_dir,
            font=self.bold_font,
            bg="#1e293b",
            fg="#e2e8f0",
            activebackground="#334155",
            activeforeground="#ffffff",
            bd=0,
            padx=15,
            pady=5,
            cursor="hand2"
        )
        self.action_btn.pack(side="right", padx=30)

    def write_log(self, text, style_tag=None):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        
    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def handle_drop(self, files):
        if self.is_running:
            return
        
        if not files:
            return
            
        file_path = files[0]
        # If passed as bytes (windnd handles decoding usually, but handle just in case)
        if isinstance(file_path, bytes):
            file_path = file_path.decode("utf-8", errors="replace")
            
        self.process_file(Path(file_path))
        
    def browse_file(self):
        if self.is_running:
            return
            
        file_path = filedialog.askopenfilename(
            title="Select Bank Statement CSV",
            filetypes=[("CSV Files", "*.csv")]
        )
        
        if file_path:
            self.process_file(Path(file_path))
            
    def process_file(self, csv_path: Path):
        if not csv_path.name.lower().endswith(".csv"):
            messagebox.showerror("Invalid File", "Please select or drop a valid .csv file.")
            return
            
        self.is_running = True
        self.status_label.config(text="Status: Executing Analysis Pipeline...", fg="#f59e0b")
        self.drop_frame.configure(highlightbackground="#f59e0b")
        self.drop_label.config(text="⏳\nRunning Fraud Detectors...\n(Please wait)", fg="#f59e0b")
        
        self.clear_log()
        self.write_log(f"Loading statement file: {csv_path.name}\n")
        self.write_log("Starting analysis pipeline...\n\n")
        
        # Launch subprocess in a background thread to keep Tkinter GUI responsive
        threading.Thread(target=self.run_pipeline, args=(csv_path,), daemon=True).start()
        
        # Start queue polling
        self.root.after(100, self.poll_logs)
        
    def run_pipeline(self, csv_path: Path):
        case_name = csv_path.stem
        output_dir = OUTPUTS / case_name
        output_dir.mkdir(exist_ok=True)

        # Determine if money trail trace credits need to be added
        trace_credits = []
        name_lower = csv_path.name.lower()
        if "mt_case_01" in name_lower:
            trace_credits = ["3545244589369467_000026"]
        elif "mt_case_02" in name_lower:
            trace_credits = ["47569602855_000029"]
        elif "mt_case_03" in name_lower:
            trace_credits = ["65805264167801_000026"]
        elif "mt_case_04" in name_lower:
            trace_credits = ["97432838969_000031"]

        cmd = [
            sys.executable, "-m", "analysis_engine.cli",
            "--input", str(csv_path),
            "--output-dir", str(output_dir),
            "--no-llm",
        ]
        if trace_credits:
            cmd.extend(["--trace-credits"] + trace_credits)
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(PROJECT_ROOT)
            )
            
            # Read stdout line by line
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                self.log_queue.put(line)
                
            process.wait()
            
            # If successful, copy report.json and report.txt to outputs/case_name/
            report_src = ANALYSIS_OP / case_name / "report.json"
            report_dest = output_dir / "report.json"
            report_txt_src = ANALYSIS_OP / case_name / "report.txt"
            report_txt_dest = output_dir / "report.txt"
            
            if process.returncode == 0:
                if report_src.exists():
                    shutil.copy2(report_src, report_dest)
                if report_txt_src.exists():
                    shutil.copy2(report_txt_src, report_txt_dest)
                self.log_queue.put(f"\n[SUCCESS] Analysis completed successfully!\n")
                self.log_queue.put(f"  SQLite DB: outputs/{case_name}/analysis.db\n")
                self.log_queue.put(f"  Report JSON: outputs/{case_name}/report.json\n")
                self.log_queue.put(f"  Report TXT: outputs/{case_name}/report.txt\n")
                # Signal completion
                self.log_queue.put(("DONE", case_name))
            else:
                self.log_queue.put(f"\n[ERROR] Pipeline failed with exit code {process.returncode}\n")
                self.log_queue.put("FAILED")
                
        except Exception as e:
            self.log_queue.put(f"\n[ERROR] Thread execution error: {e}\n")
            self.log_queue.put("FAILED")

    def poll_logs(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "FAILED":
                    self.is_running = False
                    self.status_label.config(text="Status: Pipeline Failed", fg="#ef4444")
                    self.drop_frame.configure(highlightbackground="#ef4444")
                    self.drop_label.config(text="❌\nAnalysis Failed\n(Click to try again)", fg="#ef4444")
                    return
                elif isinstance(msg, tuple) and msg[0] == "DONE":
                    self.is_running = False
                    case_name = msg[1]
                    self.status_label.config(text="Status: Analysis Finished!", fg="#10b981")
                    self.drop_frame.configure(highlightbackground="#10b981")
                    self.drop_label.config(
                        text=f"✅\nCompleted successfully!\n(Report generated for {case_name})", 
                        fg="#10b981"
                    )
                    # Automatically prompt to open outputs directory
                    if messagebox.askyesno(
                        "Analysis Complete", 
                        f"Analysis for '{case_name}' finished!\nWould you like to open the outputs folder?"
                    ):
                        self.open_case_outputs(case_name)
                    return
                else:
                    self.write_log(msg)
                    
        except queue.Empty:
            pass
            
        # Re-check queue in 100ms
        if self.is_running:
            self.root.after(100, self.poll_logs)
            
    def open_outputs_dir(self):
        try:
            os.startfile(str(OUTPUTS))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open outputs directory: {e}")
            
    def open_case_outputs(self, case_name):
        case_dir = OUTPUTS / case_name
        try:
            if case_dir.exists():
                os.startfile(str(case_dir))
            else:
                os.startfile(str(OUTPUTS))
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open case folder: {e}")

def main():
    root = tk.Tk()
    app = AnalysisGUIApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
