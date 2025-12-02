def test_langchain_imports():
    """Test T 0.2.2: import langchain, langgraph works"""
    try:
        import langchain
        import langgraph
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import PydanticOutputParser
        assert True
    except ImportError as e:
        assert False, f"LangChain imports failed: {e}"