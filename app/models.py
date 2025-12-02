from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from app.db import Base

class Resume(Base):
    __tablename__ = "resumes"
    
    id = Column(Integer, primary_key=True, index=True)
    original_filename = Column(String, nullable=False)
    raw_text = Column(Text, nullable=False)
    parsed_json = Column(Text, nullable=True)  # JSON string of parsed resume
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class JobDescription(Base):
    __tablename__ = "job_descriptions"
    
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=True)  # Null if manually entered
    raw_html = Column(Text, nullable=True)  # Null if manually entered
    extracted_text = Column(Text, nullable=False)
    parsed_json = Column(Text, nullable=True)  # JSON string of parsed JD
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class GapAnalysis(Base):
    __tablename__ = "gap_analyses"
    
    id = Column(Integer, primary_key=True, index=True)
    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("job_descriptions.id"), nullable=False)
    analysis_json = Column(Text, nullable=False)  # JSON: overlapping, missing, weak skills
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ProjectPlan(Base):
    __tablename__ = "project_plans"
    
    id = Column(Integer, primary_key=True, index=True)
    analysis_id = Column(Integer, ForeignKey("gap_analyses.id"), nullable=False)
    plan_json = Column(Text, nullable=False)  # JSON array of project ideas
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ImprovedResume(Base):
    __tablename__ = "improved_resumes"
    
    id = Column(Integer, primary_key=True, index=True)
    resume_id = Column(Integer, ForeignKey("resumes.id"), nullable=False)
    job_id = Column(Integer, ForeignKey("job_descriptions.id"), nullable=False)
    improved_json = Column(Text, nullable=False)  # JSON of Jake-style resume
    created_at = Column(DateTime(timezone=True), server_default=func.now())