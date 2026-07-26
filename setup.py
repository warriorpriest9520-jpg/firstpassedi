from setuptools import setup, find_packages

setup(
    name="firstpass-edi",
    version="1.0.0",
    description="AI-powered EDI operations platform",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="FirstPass EDI Team",
    author_email="demo@example.com",
    url="https://github.com/your-org/firstpass-edi",
    packages=find_packages(exclude=["tests*", "demo*"]),
    python_requires=">=3.11",
    install_requires=[
        "fastapi>=0.111.0",
        "uvicorn[standard]>=0.29.0",
        "pydantic>=2.7.0",
        "requests>=2.31.0",
        "httpx>=0.27.0",
        "anthropic>=0.29.0",
        "openai>=1.35.0",
        "supabase>=2.5.0",
        "python-dotenv>=1.0.0",
        "python-dateutil>=2.9.0",
    ],
    extras_require={
        "dev": [
            "pytest>=8.2.0",
            "pytest-asyncio>=0.23.0",
            "coverage>=7.5.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "firstpass-api=firstpass.api:app",
            "firstpass-orchestrator=firstpass.orchestrator:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
    ],
)
