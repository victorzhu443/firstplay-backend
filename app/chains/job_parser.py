"""
LangChain chain for parsing job description text into structured format.
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm, invoke_with_retry
from app.schemas import JobParsed

# Extraction should be deterministic; retries escalate from here.
PARSER_TEMPERATURE = 0.0

# Create the parser
parser = PydanticOutputParser(pydantic_object=JobParsed)

# Create the prompt template
job_parsing_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an expert at analyzing job descriptions. Extract key information from the job posting and return it in the specified JSON format.

Be thorough and extract:
- All required skills (must-haves)
- All preferred skills (nice-to-haves)
- Important keywords and technical terms
- Key responsibilities
- Educational and experience requirements

**Critical distinction between skills and qualifications:**
`required_skills` and `preferred_skills` must contain ONLY named technologies —
programming languages, frameworks, libraries, databases, tools, platforms,
and specific technical practices. Each entry should be a short noun phrase
that could appear in the skills section of a resume.

Everything else belongs in `qualifications`: years of experience, academic
degrees, certifications, and sentence-shaped requirements.

  Correct:   required_skills: ["Python", "FastAPI", "PostgreSQL", "Docker"]
             qualifications:  ["5+ years of professional experience",
                               "Bachelor's degree in Computer Science"]

  Incorrect: required_skills: ["Python", "5+ years experience",
                               "Bachelor's degree", "Strong communication"]

A skill list entry that is not a technology cannot be matched against a
candidate's skills, and downstream steps treat these entries as things the
candidate should learn — so "5+ years experience" would become a suggested
portfolio project.

If information is not present, use empty lists.

{format_instructions}"""),
    ("user", """Parse the following job description:

{job_text}""")
])

def create_job_parsing_chain(temperature: float = PARSER_TEMPERATURE):
    """
    Creates a LangChain runnable for parsing job descriptions.

    Args:
        temperature: Sampling temperature; raised on retry when the model's
            output fails validation

    Returns:
        A chain that takes job_text and returns JobParsed
    """
    llm = get_llm(temperature=temperature)
    
    # Create the chain: prompt | llm | parser
    chain = (
        job_parsing_prompt.partial(format_instructions=parser.get_format_instructions())
        | llm
        | parser
    )
    
    return chain

def parse_jd_text(job_text: str) -> JobParsed:
    """
    Parse job description text into structured format using LangChain.
    
    Args:
        job_text: Raw text of job description
    
    Returns:
        JobParsed: Structured job data

    Raises:
        LLMError: If parsing fails; see app.exceptions for the subtypes
    """
    return invoke_with_retry(
        create_job_parsing_chain,
        {"job_text": job_text},
        description="Failed to parse job description",
        base_temperature=PARSER_TEMPERATURE,
    )