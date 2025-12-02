"""
Pydantic schemas for structured LLM outputs.
These models define the structure we expect from LangChain parsing.
"""
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Union

class ExperienceItem(BaseModel):
    """Single work experience entry"""
    company: str = Field(description="Company name")
    title: str = Field(description="Job title")
    duration: str = Field(description="Time period (e.g., 'Jan 2020 - Present')")
    bullets: List[str] = Field(description="List of responsibility/achievement bullets")

class ProjectItem(BaseModel):
    """Single project entry"""
    name: str = Field(description="Project name")
    description: str = Field(description="Brief project description")
    technologies: List[str] = Field(description="Technologies/tools used")
    highlights: List[str] = Field(description="Key achievements or features")

class EducationItem(BaseModel):
    """Single education entry"""
    institution: str = Field(description="School/University name")
    degree: str = Field(description="Degree type and major")
    graduation_date: str = Field(description="Graduation date or expected date")
    gpa: Optional[str] = Field(default=None, description="GPA if mentioned")

class ResumeParsed(BaseModel):
    """
    Structured representation of a parsed resume.
    This is the target output format for LLM resume parsing.
    """
    name: str = Field(description="Candidate's full name")
    email: Optional[str] = Field(default=None, description="Email address")
    phone: Optional[str] = Field(default=None, description="Phone number")
    skills: List[str] = Field(description="List of technical skills")
    experience: List[ExperienceItem] = Field(description="Work experience entries")
    projects: List[ProjectItem] = Field(description="Personal/academic projects")
    education: List[EducationItem] = Field(description="Education history")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "John Doe",
                "email": "john.doe@email.com",
                "phone": "555-123-4567",
                "skills": ["Python", "JavaScript", "React", "FastAPI"],
                "experience": [
                    {
                        "company": "Tech Corp",
                        "title": "Software Engineer",
                        "duration": "Jan 2022 - Present",
                        "bullets": [
                            "Built web applications using React and FastAPI",
                            "Improved system performance by 30%"
                        ]
                    }
                ],
                "projects": [
                    {
                        "name": "E-commerce Platform",
                        "description": "Full-stack online store",
                        "technologies": ["React", "Node.js", "MongoDB"],
                        "highlights": ["Implemented payment processing", "Built admin dashboard"]
                    }
                ],
                "education": [
                    {
                        "institution": "University of Example",
                        "degree": "BS in Computer Science",
                        "graduation_date": "May 2021",
                        "gpa": "3.8"
                    }
                ]
            }
        }
    )
class JobParsed(BaseModel):
    """
    Structured representation of a parsed job description.
    This is the target output format for LLM job description parsing.
    """
    job_title: str = Field(description="Job title/position")
    company: Optional[str] = Field(default=None, description="Company name if mentioned")
    required_skills: List[str] = Field(description="Required/must-have skills and qualifications")
    preferred_skills: List[str] = Field(description="Preferred/nice-to-have skills")
    keywords: List[str] = Field(description="Important keywords and technical terms from the JD")
    responsibilities: List[str] = Field(description="Key job responsibilities")
    qualifications: List[str] = Field(description="Educational or experience requirements")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_title": "Senior Software Engineer",
                "company": "Tech Corp",
                "required_skills": [
                    "Python",
                    "FastAPI",
                    "PostgreSQL",
                    "5+ years experience"
                ],
                "preferred_skills": [
                    "AWS",
                    "Docker",
                    "React",
                    "Machine Learning"
                ],
                "keywords": [
                    "backend development",
                    "REST APIs",
                    "microservices",
                    "agile",
                    "CI/CD"
                ],
                "responsibilities": [
                    "Design and develop backend services",
                    "Lead technical architecture decisions",
                    "Mentor junior developers",
                    "Collaborate with product team"
                ],
                "qualifications": [
                    "Bachelor's degree in Computer Science or related field",
                    "5+ years of professional software development experience",
                    "Strong understanding of database design"
                ]
            }
        }
    )
class ProjectIdea(BaseModel):
    """A single project idea to help close skill gaps"""
    title: str = Field(description="Project title")
    skill_targets: List[str] = Field(description="Skills this project helps develop")
    difficulty: str = Field(description="Difficulty level: Beginner, Intermediate, or Advanced")
    description: str = Field(description="Brief project description (2-3 sentences)")
    estimated_duration: str = Field(description="Estimated time to complete (e.g., '2-3 weeks')")
    key_features: List[str] = Field(description="3-5 key features to implement")
    technologies: List[str] = Field(description="Technologies and tools to use")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "RESTful Task Management API",
                "skill_targets": ["FastAPI", "PostgreSQL", "Docker"],
                "difficulty": "Intermediate",
                "description": "Build a complete REST API for task management with user authentication, CRUD operations, and database integration. This project will strengthen your backend development skills.",
                "estimated_duration": "2-3 weeks",
                "key_features": [
                    "User authentication with JWT",
                    "CRUD operations for tasks",
                    "PostgreSQL database integration",
                    "Docker containerization",
                    "API documentation with Swagger"
                ],
                "technologies": ["Python", "FastAPI", "PostgreSQL", "Docker", "JWT"]
            }
        }
    )

class ProjectPlanParsed(BaseModel):
    """Collection of project ideas"""
    projects: List[ProjectIdea] = Field(description="List of recommended projects")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "projects": [
                    {
                        "title": "RESTful Task Management API",
                        "skill_targets": ["FastAPI", "PostgreSQL"],
                        "difficulty": "Intermediate",
                        "description": "Build a REST API for task management.",
                        "estimated_duration": "2-3 weeks",
                        "key_features": ["Authentication", "CRUD operations", "Database"],
                        "technologies": ["Python", "FastAPI", "PostgreSQL"]
                    }
                ]
            }
        }
    )

class ImprovedExperienceItem(BaseModel):
    """Improved work experience entry with Jake-style bullets"""
    company: str = Field(description="Company name")
    title: str = Field(description="Job title")
    duration: str = Field(description="Time period")
    bullets: List[str] = Field(description="Achievement bullets using action verbs, tech/context, and metrics")

class ImprovedProjectItem(BaseModel):
    """Improved project entry with Jake-style bullets"""
    name: str = Field(description="Project name")
    technologies: List[str] = Field(description="Technologies used")
    bullets: List[str] = Field(description="Achievement bullets using action verbs, tech/context, and metrics")

class ImprovedResumeParsed(BaseModel):
    """Improved resume in Jake's template format"""
    name: str
    contact: str
    summary: Optional[str] = None
    skills: List[str]
    experience: List[ImprovedExperienceItem]
    projects: List[ImprovedProjectItem]
    education: List[Union[str, dict]]  # Accept both string and dict format
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "JOHN DOE",
                "contact": "john.doe@email.com | 555-123-4567",
                "summary": "Software engineer with 3 years of experience...",
                "skills": ["Python", "FastAPI", "React"],
                "experience": [{
                    "company": "Tech Corp",
                    "title": "Software Engineer",
                    "duration": "2021-2024",
                    "bullets": ["Built APIs..."]
                }],
                "projects": [{
                    "name": "E-commerce Platform",
                    "technologies": ["React", "Node.js"],
                    "bullets": ["Developed..."]
                }],
                "education": ["Cornell University, BS Computer Science, May 2027"]
            }
        }
    )