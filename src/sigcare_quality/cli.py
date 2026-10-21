from pathlib import Path
import typer
from rich import print
from .pipeline import run_quality_pipeline

app = typer.Typer(help="Local Sigcare PDF integrity checks and image cleanup.")

@app.command()
def check(input_pdf: Path = typer.Argument(..., exists=True, readable=True),
          output: Path = typer.Option(Path("output"), "--output", "-o"),
          dpi: int = typer.Option(220, "--dpi", min=100, max=400),
          no_cleanup: bool = typer.Option(False, "--no-cleanup")):
    report = run_quality_pipeline(input_pdf, output, dpi=dpi, create_cleanup=not no_cleanup)
    status = report.get("overall_status", "ERROR")
    colour = "green" if status == "PASS" else "yellow" if status == "REVIEW" else "red"
    print(f"[{colour}]Overall status: {status}[/{colour}]")
    print(f"Pages checked: {len(report.get('pages', []))}")
    print(f"Report: {(output / 'quality_report.txt').resolve()}")
    print(f"JSON: {(output / 'quality_report.json').resolve()}")
    if not no_cleanup:
        print(f"Cleaned OCR copies: {(output / 'cleaned_pages').resolve()}")
    print("[yellow]PASS is not proof of authenticity. Review flagged pages against the original.[/yellow]")

if __name__ == "__main__":
    app()
