"""
LangChain model wrapper for LLM interactions.
Uses LangChain ChatOpenAI instead of direct OpenAI SDK calls.
"""
import os
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def get_llm(model: str = "gpt-4o-mini", temperature: float = 0.0):
    """
    Returns a LangChain ChatModel instance.
    
    Args:
        model: The OpenAI model to use (default: gpt-4o-mini)
        temperature: Creativity level 0.0-1.0 (default: 0.0 for consistency)
    
    Returns:
        ChatOpenAI: A LangChain chat model instance
    
    Raises:
        ValueError: If OPENAI_API_KEY is not set
    """
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY environment variable is not set. "
            "Please add it to your .env file."
        )
    
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=api_key
    )