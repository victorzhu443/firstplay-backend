"""
LangChain chain for improving resumes using Jake's template.
Rewrites bullets to follow: Action Verb + Technical Context + Metric/Impact
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm
from app.schemas import ResumeParsed, JobParsed, ImprovedResumeParsed
from typing import Dict

# Create the parser
parser = PydanticOutputParser(pydantic_object=ImprovedResumeParsed)

# Create the prompt template
resume_improvement_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an expert resume writer and career coach. Your job is to rewrite resumes using Jake's Resume Template format.

**Jake's Template Rules:**
1. Every bullet point follows this format: ACTION VERB + TECHNICAL CONTEXT + METRIC/IMPACT
2. Start with strong action verbs (Built, Developed, Implemented, Designed, Optimized, etc.)
3. Include specific technologies and tools used
4. Always include quantifiable metrics or impact (%, $, numbers, time saved, users affected, etc.)

**Your Task:**
- Rewrite the candidate's resume to be tailored for the specific job
- Highlight skills that match the job requirements
- Transform weak bullets into strong Jake-style bullets with metrics
- Add technical context and quantifiable achievements
- Prioritize job-required skills in the skills section
- Keep the same experience/projects but improve the descriptions

**Examples of Jake-style bullets:**
❌ Bad: "Worked on backend development"
✅ Good: "Developed RESTful APIs using Python and FastAPI, reducing response time by 40% and serving 50K+ daily requests"

❌ Bad: "Built a web application"
✅ Good: "Architected full-stack e-commerce platform with React and Node.js, processing $100K+ in monthly transactions with 99.9% uptime"

{format_instructions}"""),
    ("user", """**Original Resume:**
{resume_data}

**Target Job Description:**
Title: {job_title}
Required Skills: {required_skills}
Preferred Skills: {preferred_skills}

**Skill Gap Analysis:**
Skills candidate has: {overlapping_skills}
Skills candidate lacks: {missing_skills}

Please rewrite this resume to be highly tailored for this job. Focus on:
1. Highlighting the {overlapping_skills} the candidate already has
2. Using Jake's template format for all bullets (action verb + tech + metric)
3. Making achievements quantifiable and impactful
4. Prioritizing job-required skills""")
])

def create_resume_improvement_chain():
    """
    Creates a LangChain runnable for improving resumes.
    
    Returns:
        A chain that takes resume, job, and gap data and returns ImprovedResumeParsed
    """
    llm = get_llm(temperature=0.3)  # Low temperature for consistency but some creativity
    
    # Create the chain: prompt | llm | parser
    chain = (
        resume_improvement_prompt.partial(format_instructions=parser.get_format_instructions())
        | llm
        | parser
    )
    
    return chain

def improve_resume(
    resume: ResumeParsed,
    job: JobParsed,
    gap_analysis: Dict
) -> ImprovedResumeParsed:
    """
    Improve a resume by tailoring it to a specific job using Jake's template.
    
    Args:
        resume: Parsed resume data
        job: Parsed job description data
        gap_analysis: Gap analysis results with overlapping/missing skills
    
    Returns:
        ImprovedResumeParsed: Improved resume with Jake-style bullets
    
    Raises:
        Exception: If improvement fails
    """
    chain = create_resume_improvement_chain()
    
    # Format data for the prompt
    resume_data = resume.model_dump_json(indent=2)
    job_title = job.job_title
    required_skills = ", ".join(job.required_skills)
    preferred_skills = ", ".join(job.preferred_skills) if job.preferred_skills else "None"
    overlapping_skills = ", ".join(gap_analysis.get("overlapping_skills", []))
    missing_skills = ", ".join(
        gap_analysis.get("missing_required_skills", []) + 
        gap_analysis.get("missing_preferred_skills", [])
    )
    
    try:
        result = chain.invoke({
            "resume_data": resume_data,
            "job_title": job_title,
            "required_skills": required_skills,
            "preferred_skills": preferred_skills,
            "overlapping_skills": overlapping_skills,
            "missing_skills": missing_skills
        })
        return result
    except Exception as e:
        raise Exception(f"Failed to improve resume: {str(e)}")