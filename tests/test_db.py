from app.db import SessionLocal, engine

def test_session_open_close():
    """Test that we can open and close a database session without errors"""
    db = SessionLocal()
    try:
        # If this runs without error, the session works
        assert db is not None
    finally:
        db.close()

def test_engine_connection():
    """Test that the engine can connect"""
    connection = engine.connect()
    assert connection is not None
    connection.close()

from app.db import SessionLocal, engine, Base
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume

def test_session_open_close():
    """Test that we can open and close a database session without errors"""
    db = SessionLocal()
    try:
        # If this runs without error, the session works
        assert db is not None
    finally:
        db.close()

def test_engine_connection():
    """Test that the engine can connect"""
    connection = engine.connect()
    assert connection is not None
    connection.close()

def test_create_tables():
    """Test T 2.2.1: Base.metadata.create_all(engine) runs successfully"""
    try:
        Base.metadata.create_all(bind=engine)
        # If no exception is raised, the test passes
        assert True
    except Exception as e:
        assert False, f"Failed to create tables: {e}"

def test_insert_and_query_resume():
    """Test T 2.2.2: Insert a Resume row, commit, and query it back"""
    # Create tables first
    Base.metadata.create_all(bind=engine)
    
    # Create a session
    db = SessionLocal()
    
    try:
        # Create a test resume
        test_resume = Resume(
            original_filename="test_resume.pdf",
            raw_text="This is a test resume with some sample text."
        )
        
        # Add and commit
        db.add(test_resume)
        db.commit()
        db.refresh(test_resume)
        
        # Verify it has an ID
        assert test_resume.id is not None
        
        # Query it back
        queried_resume = db.query(Resume).filter(Resume.id == test_resume.id).first()
        
        # Verify the data matches
        assert queried_resume is not None
        assert queried_resume.original_filename == "test_resume.pdf"
        assert queried_resume.raw_text == "This is a test resume with some sample text."
        assert queried_resume.id == test_resume.id
        
        # Clean up - delete the test resume
        db.delete(test_resume)
        db.commit()
        
    finally:
        db.close()