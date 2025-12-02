"""
LangChain chain for parsing resume text into structured format.
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm
from app.schemas import ResumeParsed

# Create the parser
parser = PydanticOutputParser(pydantic_object=ResumeParsed)

# Create the prompt template
resume_parsing_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are an expert resume parser. Extract information from the resume text and return it in the specified JSON format.

Be thorough and accurate. If information is not present, use empty lists or null values.

{format_instructions}"""),
    ("user", """Parse the following resume text:

{resume_text}""")
])

def create_resume_parsing_chain():
    """
    Creates a LangChain runnable for parsing resumes.
    
    Returns:
        A chain that takes resume_text and returns ResumeParsed
    """
    llm = get_llm()
    
    # Create the chain: prompt | llm | parser
    chain = (
        resume_parsing_prompt.partial(format_instructions=parser.get_format_instructions())
        | llm
        | parser
    )
    
    return chain

def parse_resume_text(resume_text: str) -> ResumeParsed:
    """
    Parse resume text into structured format using LangChain.
    
    Args:
        resume_text: Raw text extracted from resume PDF
    
    Returns:
        ResumeParsed: Structured resume data
    
    Raises:
        Exception: If parsing fails
    """
    chain = create_resume_parsing_chain()
    
    try:
        result = chain.invoke({"resume_text": resume_text})
        return result
    except Exception as e:
        raise Exception(f"Failed to parse resume: {str(e)}")