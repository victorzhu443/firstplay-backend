def test_langchain_imports():
    """Test T 0.2.2: the LangChain packages the app uses import.

    The `langchain` meta-package itself is not one of them: the app imports
    langchain_core, langchain_openai, langchain_cohere and langgraph only, and
    requirements.txt stopped pinning the unused meta-package (RC-7)."""
    try:
        import langchain_core
        import langgraph
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import PydanticOutputParser
        assert True
    except ImportError as e:
        assert False, f"LangChain imports failed: {e}"