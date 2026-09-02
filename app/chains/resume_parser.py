"""
LangChain chain for parsing resume text into structured format.
"""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from app.llm_client import get_llm, invoke_with_retry
from app.schemas import ResumeParsed

# Extraction should be deterministic; retries escalate from here.
PARSER_TEMPERATURE = 0.0

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

def create_resume_parsing_chain(temperature: float = PARSER_TEMPERATURE):
    """
    Creates a LangChain runnable for parsing resumes.

    Args:
        temperature: Sampling temperature; raised on retry when the model's
            output fails validation

    Returns:
        A chain that takes resume_text and returns ResumeParsed
    """
    llm = get_llm(temperature=temperature)
    
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
        LLMError: If parsing fails; see app.exceptions for the subtypes
    """
    return invoke_with_retry(
        create_resume_parsing_chain,
        {"resume_text": resume_text},
        description="Failed to parse resume",
        base_temperature=PARSER_TEMPERATURE,
    )