from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import resume, job, analysis, pipeline

app = FastAPI(
    title="FirstPlay Coach API",
    description="Resume analysis and project planning for early-career CS students",
    version="0.1.0"
)

# CORS middleware for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://firstplay-frontend.vercel.app",  # Your production domain
        "https://*.vercel.app"  # All Vercel preview deployments
    ],
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

