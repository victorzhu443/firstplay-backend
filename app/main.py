from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import resume, job, analysis, pipeline
from app.db import engine, Base
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume

app = FastAPI(
    title="FirstPlay Coach API",
    description="Resume analysis and project planning for early-career CS students",
    version="0.1.0"
)

# Create tables on startup
@app.on_event("startup")
async def startup_event():
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created!")

# CORS middleware.
# `allow_origins` is matched by exact string comparison, so the previous
# "https://*.vercel.app" entry matched nothing and every Vercel preview
# deployment was silently blocked. Wildcards belong in allow_origin_regex.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://firstplay-frontend.vercel.app",
    ],
    # Vercel preview deployments: <project>-<hash>-<scope>.vercel.app
    allow_origin_regex=r"https://[a-zA-Z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(resume.router)
app.include_router(job.router)
app.include_router(analysis.router)
app.include_router(pipeline.router)

@app.get("/")
def read_root():
    return {
        "message": "Welcome to FirstPlay Coach API",
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
def health_check():
    return {"status": "ok"}

