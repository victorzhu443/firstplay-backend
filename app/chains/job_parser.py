"""
LangChain chain for parsing job description text into structured format.
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm
from app.schemas import JobParsed

# Create the parser
parser = PydanticOutputParser(pydantic_object=JobParsed)

# Create the prompt template
job_parsing_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an expert at analyzing job descriptions. Extract key information from the job posting and return it in the specified JSON format.

Be thorough and extract:
- All required skills and qualifications (must-haves)
- All preferred skills (nice-to-haves)
- Important keywords and technical terms
- Key responsibilities
- Educational and experience requirements

If information is not present, use empty lists.

{format_instructions}"""),
    ("user", """Parse the following job description:

{job_text}""")
])

def create_job_parsing_chain():
    """
    Creates a LangChain runnable for parsing job descriptions.
    
    Returns:
        A chain that takes job_text and returns JobParsed
    """
    llm = get_llm()
    
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
        Exception: If parsing fails
    """
    chain = create_job_parsing_chain()
    
    try:
        result = chain.invoke({"job_text": job_text})
        return result
    except Exception as e:
        raise Exception(f"Failed to parse job description: {str(e)}")