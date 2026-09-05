"""
CareerOS AI — FastAPI application entry point.

Run from the project root:
    python src/main.py

Then open http://127.0.0.1:8000 in your browser.
Interactive API docs: http://127.0.0.1:8000/docs
"""
import sys, os sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import agents
import config
from github_service import GitHubError, fetch_profile
from qwen_client import GeminiClient, GeminiError
from resume_service import ResumeError, extract_text_from_pdf

# Folders -------------------------------------------------------------------
SRC_DIR = Path(__file__).resolve().parent
STATIC_DIR = SRC_DIR / "static"

# App -----------------------------------------------------------------------
app = FastAPI(
    title=config.settings.app_name,
    description=(
        "Multi-agent career intelligence platform: analyzes a resume and a "
        "GitHub profile with 5 Gemini-powered agents and returns an "
        "evidence-based career readiness report."
    ),
    version=config.settings.app_version,
)


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Lightweight status check (also used by the frontend at load)."""
    return {
        "status": "ok",
        "app": config.settings.app_name,
        "version": config.settings.app_version,
        "model": config.settings.gemini_model,
        "gemini_configured": config.is_gemini_configured(),
        "github_token_set": bool(config.settings.github_token.strip()),
    }


@app.post("/api/analyze")
def analyze(
    resume: UploadFile = File(..., description="Resume as a PDF file"),
    github_username: str = Form(..., description="Public GitHub username"),
    target_role: str = Form(..., description="Role the candidate is targeting"),
    job_description: Optional[str] = Form(
        None, description="Optional job description to match against"
    ),
) -> dict:
    """
    Run the full 5-agent analysis.

    This endpoint is a plain (sync) function on purpose: FastAPI runs sync
    endpoints in a worker thread, so the ~1-2 minutes of LLM calls do not
    block the rest of the server.
    """
    # -- 1. Validate inputs -------------------------------------------------
    github_username = (github_username or "").strip()
    target_role = (target_role or "").strip()
    job_description = (job_description or "").strip()

    if not github_username:
        raise HTTPException(status_code=400, detail="GitHub username is required.")
    if not target_role:
        raise HTTPException(status_code=400, detail="Target role is required.")

    filename = (resume.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(
            status_code=400, detail="Please upload your resume as a PDF file."
        )

    resume_bytes = resume.file.read()
    max_bytes = int(config.settings.max_resume_mb * 1024 * 1024)
    if len(resume_bytes) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"Resume is too large. Maximum size is {config.settings.max_resume_mb:.0f} MB.",
        )
    if not resume_bytes:
        raise HTTPException(status_code=400, detail="The uploaded resume file is empty.")

    if not config.is_gemini_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "The AI model is not configured yet. Copy .env.example to .env "
                "and set GOOGLE_API_KEY with your Google AI Studio key."
            ),
        )

    # -- 2. Gather evidence -------------------------------------------------
    try:
        resume_text = extract_text_from_pdf(resume_bytes)
    except ResumeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        github_profile = fetch_profile(github_username)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # -- 3. Run the agent pipeline ------------------------------------------
    try:
        llm = GeminiClient()
        results = agents.run_full_analysis(
            llm=llm,
            resume_text=resume_text,
            github_profile=github_profile,
            target_role=target_role,
            job_description=job_description,
        )
    except GeminiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # -- 4. Respond ----------------------------------------------------------
    return {
        "status": "success",
        "target_role": target_role,
        "github_username": github_username,
        # The headline report the UI renders:
        "analysis": results["career_report"],
        # Each specialist agent's raw output (shown in the UI, useful for demos):
        "agent_details": {
            "resume_analysis": results["resume_analysis"],
            "github_analysis": results["github_analysis"],
            "job_match": results["job_match"],
            "skill_gaps": results["skill_gaps"],
        },
    }


if __name__ == "__main__":
    # Allow `python src/main.py` to start the dev server directly.
    import uvicorn

    uvicorn.run(
        app,
        host=config.settings.api_host,
        port=config.settings.api_port,
        reload=False,
    )
