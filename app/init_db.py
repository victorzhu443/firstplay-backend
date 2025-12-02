from app.db import engine, Base
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume

def init_db():
    """
    Initialize the database by creating all tables.
    This is idempotent - it won't recreate tables that already exist.
    """
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("✓ Database tables created successfully!")
    print(f"✓ Database location: firstplay.db")
    print("\nTables created:")
    print("  - resumes")
    print("  - job_descriptions")
    print("  - gap_analyses")
    print("  - project_plans")
    print("  - improved_resumes")

if __name__ == "__main__":
    init_db()