"""
LangChain chain for generating project ideas based on skill gaps.
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm
from app.schemas import ProjectPlanParsed
from typing import Dict

# Create the parser
parser = PydanticOutputParser(pydantic_object=ProjectPlanParsed)

# Create the prompt template
project_generation_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an expert career coach and software engineering mentor. 
Your job is to create practical, achievable project ideas that will help students develop specific skills.

Create 3-5 project ideas that:
- Target the missing skills the student needs
- Are appropriate for their current skill level
- Are realistic and can be completed in 1-6 weeks
- Include specific technical details
- Build on their existing skills when possible

Each project should be practical, portfolio-worthy, and teach real-world development skills.

{format_instructions}"""),
    ("user", """Based on the following skill gap analysis, generate project ideas:

**Skills the student currently has:**
{overlapping_skills}

**Required skills the student is missing:**
{missing_required_skills}

**Preferred skills the student is missing:**
{missing_preferred_skills}

Please generate 3-5 project ideas that will help this student develop the missing skills, especially the required ones.""")
])

def create_project_generation_chain():
    """
    Creates a LangChain runnable for generating project ideas.
    
    Returns:
        A chain that takes gap analysis and returns ProjectPlanParsed
    """
    llm = get_llm(temperature=0.7)  # Higher temperature for more creative ideas
    
    # Create the chain: prompt | llm | parser
    chain = (
        project_generation_prompt.partial(format_instructions=parser.get_format_instructions())
        | llm
        | parser
    )
    
    return chain

def generate_projects(gap_analysis: Dict) -> ProjectPlanParsed:
    """
    Generate project ideas based on skill gap analysis.
    
    Args:
        gap_analysis: Dictionary containing:
            - overlapping_skills: List of skills the candidate has
            - missing_required_skills: Required skills they lack
            - missing_preferred_skills: Preferred skills they lack
    
    Returns:
        ProjectPlanParsed: List of project ideas
    
    Raises:
        Exception: If generation fails
    """
    chain = create_project_generation_chain()
    
    # Format skills for the prompt
    overlapping = ", ".join(gap_analysis.get("overlapping_skills", [])) or "None"
    missing_required = ", ".join(gap_analysis.get("missing_required_skills", [])) or "None"
    missing_preferred = ", ".join(gap_analysis.get("missing_preferred_skills", [])) or "None"
    
    try:
        result = chain.invoke({
            "overlapping_skills": overlapping,
            "missing_required_skills": missing_required,
            "missing_preferred_skills": missing_preferred
        })
        return result
    except Exception as e:
        raise Exception(f"Failed to generate projects: {str(e)}")