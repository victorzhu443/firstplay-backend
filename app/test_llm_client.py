import pytest
import os
from app.llm_client import get_llm
from langchain_openai import ChatOpenAI

def test_get_llm_returns_chatmodel():
    """Test T 4.1.1: get_llm() returns LangChain ChatModel"""
    llm = get_llm()
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "gpt-4o-mini"
    assert llm.temperature == 0.0

def test_missing_api_key_raises_error():
    """Test T 4.1.2: missing env vars raise clean error"""
    # Save original API key
    original_key = os.getenv("OPENAI_API_KEY")
    
    try:
        # Temporarily remove API key
        if "OPENAI_API_KEY" in os.environ:
            del os.environ["OPENAI_API_KEY"]
        
        with pytest.raises(ValueError) as exc_info:
            get_llm()
        
        assert "OPENAI_API_KEY" in str(exc_info.value)
        assert "not set" in str(exc_info.value)
    
    finally:
        # Restore original API key
        if original_key:
            os.environ["OPENAI_API_KEY"] = original_key

def test_custom_model_and_temperature():
    """Test that custom parameters work"""
    llm = get_llm(model="gpt-4o", temperature=0.7)
    assert llm.model_name == "gpt-4o"
    assert llm.temperature == 0.7