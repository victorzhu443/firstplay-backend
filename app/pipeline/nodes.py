"""
LangGraph nodes for the FirstPlay Coach pipeline.
Each node performs one step of the workflow.
"""
from sqlalchemy.orm import Session
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume
from app.chains.resume_parser import parse_resume_text
from app.chains.job_parser import parse_jd_text
from app.analysis.gap_analysis import compute_gap
from app.chains.project_generator import generate_projects
from app.chains.resume_improver import improve_resume
from app.pipeline.state import PipelineState
import json

def parse_resume_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 1: Parse resume from database
    """
    try:
        resume = db.query(Resume).filter(Resume.id == state["resume_id"]).first()
        
        if not resume:
            state["error"] = f"Resume {state['resume_id']} not found"
            return state
        
        # Parse if not already parsed
        if not resume.parsed_json:
            parsed = parse_resume_text(resume.raw_text)
            resume.parsed_json = parsed.model_dump_json()
            db.commit()
            db.refresh(resume)
            state["resume_parsed"] = parsed
        else:
            # Load from database
            state["resume_parsed"] = parse_resume_text.__annotations__['return']
            state["resume_parsed"] = parse_resume_text.__annotations__['return'].model_validate_json(resume.parsed_json)
        
        return state
    
    except Exception as e:
        state["error"] = f"Error parsing resume: {str(e)}"
        return state

def parse_job_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 2: Parse job description from database
    """
    try:
        job = db.query(JobDescription).filter(JobDescription.id == state["job_id"]).first()
        
        if not job:
            state["error"] = f"Job {state['job_id']} not found"
            return state
        
        # Parse if not already parsed
        if not job.parsed_json:
            parsed = parse_jd_text(job.extracted_text)
            job.parsed_json = parsed.model_dump_json()
            db.commit()
            db.refresh(job)
            state["job_parsed"] = parsed
        else:
            # Load from database
            from app.schemas import JobParsed
            state["job_parsed"] = JobParsed.model_validate_json(job.parsed_json)
        
        return state
    
    except Exception as e:
        state["error"] = f"Error parsing job: {str(e)}"
        return state

def analyze_gap_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 3: Compute gap analysis
    """
    try:
        resume_parsed = state["resume_parsed"]
        job_parsed = state["job_parsed"]
        
        # Compute gap
        gap_result = compute_gap(resume_parsed, job_parsed)
        state["gap_analysis"] = gap_result
        
        # Save to database
        gap_analysis = GapAnalysis(
            resume_id=state["resume_id"],
            job_id=state["job_id"],
            analysis_json=json.dumps(gap_result)
        )
        db.add(gap_analysis)
        db.commit()
        db.refresh(gap_analysis)
        
        state["analysis_id"] = gap_analysis.id
        
        return state
    
    except Exception as e:
        state["error"] = f"Error in gap analysis: {str(e)}"
        return state

def generate_projects_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 4: Generate project ideas
    """
    try:
        gap_data = state["gap_analysis"]
        
        # Generate projects
        project_plan = generate_projects(gap_data)
        state["projects"] = project_plan
        
        # Save to database
        project_plan_record = ProjectPlan(
            analysis_id=state["analysis_id"],
            plan_json=project_plan.model_dump_json()
        )
        db.add(project_plan_record)
        db.commit()
        db.refresh(project_plan_record)
        
        state["project_plan_id"] = project_plan_record.id
        
        return state
    
    except Exception as e:
        state["error"] = f"Error generating projects: {str(e)}"
        return state

def improve_resume_node(state: PipelineState, db: Session) -> PipelineState:
    """
    Node 5: Improve resume
    """
    try:
        resume_parsed = state["resume_parsed"]
        job_parsed = state["job_parsed"]
        gap_data = state["gap_analysis"]
        
        # Improve resume
        improved = improve_resume(resume_parsed, job_parsed, gap_data)
        state["improved_resume"] = improved
        
        # Save to database
        improved_resume = ImprovedResume(
            resume_id=state["resume_id"],
            job_id=state["job_id"],
            improved_json=improved.model_dump_json()
        )
        db.add(improved_resume)
        db.commit()
        db.refresh(improved_resume)
        
        state["improved_resume_id"] = improved_resume.id
        
        return state
    
    except Exception as e:
        state["error"] = f"Error improving resume: {str(e)}"
        return state