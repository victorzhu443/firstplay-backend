import pytest
from app.schemas import ResumeParsed, ExperienceItem, ProjectItem, EducationItem
from pydantic import ValidationError

def test_resume_parsed_validates_with_complete_data():
    """Test T 4.2.1: sample JSON validates"""
    sample_data = {
        "name": "Jane Smith",
        "email": "jane@example.com",
        "phone": "555-0123",
        "skills": ["Python", "React", "AWS"],
        "experience": [
            {
                "company": "StartupXYZ",
                "title": "Junior Developer",
                "duration": "2022-2024",
                "bullets": ["Developed features", "Fixed bugs"]
            }
        ],
        "projects": [
            {
                "name": "Portfolio Site",
                "description": "Personal website",
                "technologies": ["Next.js", "Tailwind"],
                "highlights": ["Responsive design"]
            }
        ],
        "education": [
            {
                "institution": "State University",
                "degree": "BS Computer Science",
                "graduation_date": "2022"
            }
        ]
    }
    
    # This should not raise any errors
    resume = ResumeParsed(**sample_data)
    assert resume.name == "Jane Smith"
    assert len(resume.skills) == 3
    assert len(resume.experience) == 1
    assert resume.experience[0].company == "StartupXYZ"

def test_resume_parsed_fails_with_missing_required_fields():
    """Test T 4.2.2: missing fields fail"""
    incomplete_data = {
        "name": "John Doe",
        # Missing required fields: skills, experience, projects, education
    }
    
    with pytest.raises(ValidationError) as exc_info:
        ResumeParsed(**incomplete_data)
    
    # Check that validation errors mention the missing fields
    error_msg = str(exc_info.value)
    assert "skills" in error_msg or "field required" in error_msg.lower()

def test_resume_parsed_with_optional_fields():
    """Test that optional fields (email, phone) work"""
    minimal_data = {
        "name": "Bob Johnson",
        # email and phone are optional
        "skills": ["Java"],
        "experience": [],
        "projects": [],
        "education": []
    }
    
    resume = ResumeParsed(**minimal_data)
    assert resume.name == "Bob Johnson"
    assert resume.email is None
    assert resume.phone is None
    assert len(resume.skills) == 1

def test_experience_item_validation():
    """Test ExperienceItem model"""
    exp = ExperienceItem(
        company="Google",
        title="SWE Intern",
        duration="Summer 2023",
        bullets=["Worked on Search", "Shipped feature"]
    )
    assert exp.company == "Google"
    assert len(exp.bullets) == 2

def test_project_item_validation():
    """Test ProjectItem model"""
    project = ProjectItem(
        name="Chat App",
        description="Real-time messaging",
        technologies=["WebSocket", "Redis"],
        highlights=["100+ users", "Low latency"]
    )
    assert project.name == "Chat App"
    assert "WebSocket" in project.technologies


from app.schemas import JobParsed

def test_job_parsed_validates_with_complete_data():
    """Test T 7.1.1: Sample JD JSON validates as JobParsed"""
    sample_data = {
        "job_title": "Backend Developer",
        "company": "Startup Inc",
        "required_skills": ["Python", "Django", "PostgreSQL"],
        "preferred_skills": ["AWS", "Docker"],
        "keywords": ["REST API", "microservices", "agile"],
        "responsibilities": [
            "Build scalable APIs",
            "Write unit tests",
            "Code reviews"
        ],
        "qualifications": [
            "BS in Computer Science",
            "3+ years Python experience"
        ]
    }
    
    job = JobParsed(**sample_data)
    assert job.job_title == "Backend Developer"
    assert job.company == "Startup Inc"
    assert "Python" in job.required_skills
    assert len(job.responsibilities) == 3

def test_job_parsed_fails_with_missing_required_fields():
    """Test that missing required fields fail validation"""
    incomplete_data = {
        "job_title": "Software Engineer",
        # Missing: required_skills, preferred_skills, keywords, responsibilities, qualifications
    }
    
    with pytest.raises(ValidationError) as exc_info:
        JobParsed(**incomplete_data)
    
    error_msg = str(exc_info.value)
    assert "field required" in error_msg.lower()

def test_job_parsed_with_optional_company():
    """Test that company field is optional"""
    minimal_data = {
        "job_title": "Developer",
        "required_skills": ["JavaScript"],
        "preferred_skills": ["TypeScript"],
        "keywords": ["frontend"],
        "responsibilities": ["Build UIs"],
        "qualifications": ["2+ years experience"]
    }
    
    job = JobParsed(**minimal_data)
    assert job.job_title == "Developer"
    assert job.company is None  # Should be None when not provided
    assert len(job.required_skills) == 1

def test_job_parsed_empty_lists_allowed():
    """Test that empty lists are valid"""
    data = {
        "job_title": "Junior Developer",
        "required_skills": ["Python"],
        "preferred_skills": [],  # Empty is valid
        "keywords": ["backend"],
        "responsibilities": ["Learn and grow"],
        "qualifications": []  # Empty is valid
    }
    
    job = JobParsed(**data)
    assert len(job.preferred_skills) == 0
    assert len(job.qualifications) == 0

from app.schemas import ProjectIdea, ProjectPlanParsed

def test_project_idea_validates():
    """Test T 9.1.1: Sample project validates"""
    project_data = {
        "title": "E-commerce Backend",
        "skill_targets": ["FastAPI", "PostgreSQL", "Redis"],
        "difficulty": "Advanced",
        "description": "Build a scalable e-commerce backend with product catalog, shopping cart, and order processing.",
        "estimated_duration": "4-6 weeks",
        "key_features": [
            "Product catalog with search",
            "Shopping cart management",
            "Order processing",
            "Payment integration"
        ],
        "technologies": ["Python", "FastAPI", "PostgreSQL", "Redis", "Stripe"]
    }
    
    project = ProjectIdea(**project_data)
    assert project.title == "E-commerce Backend"
    assert "FastAPI" in project.skill_targets
    assert project.difficulty == "Advanced"
    assert len(project.key_features) == 4

def test_project_plan_parsed_validates():
    """Test that ProjectPlanParsed validates with list of projects"""
    plan_data = {
        "projects": [
            {
                "title": "Todo API",
                "skill_targets": ["FastAPI"],
                "difficulty": "Beginner",
                "description": "Simple todo API",
                "estimated_duration": "1 week",
                "key_features": ["CRUD", "API"],
                "technologies": ["Python", "FastAPI"]
            },
            {
                "title": "Blog Platform",
                "skill_targets": ["React", "Node.js"],
                "difficulty": "Intermediate",
                "description": "Full-stack blog",
                "estimated_duration": "3 weeks",
                "key_features": ["Posts", "Comments", "Auth"],
                "technologies": ["React", "Node.js", "MongoDB"]
            }
        ]
    }
    
    plan = ProjectPlanParsed(**plan_data)
    assert len(plan.projects) == 2
    assert plan.projects[0].title == "Todo API"
    assert plan.projects[1].difficulty == "Intermediate"

def test_project_idea_missing_fields():
    """Test that missing required fields fail validation"""
    incomplete_data = {
        "title": "Test Project",
        # Missing: skill_targets, difficulty, description, etc.
    }
    
    with pytest.raises(ValidationError):
        ProjectIdea(**incomplete_data)

from app.schemas import ImprovedResumeParsed, ImprovedExperienceItem, ImprovedProjectItem

def test_improved_resume_validates():
    """Test T 10.1.1: Sample improved resume JSON validates as ImprovedResumeParsed"""
    improved_data = {
        "name": "Jane Smith",
        "contact": "jane@email.com | 555-0123",
        "summary": "Backend developer with 5 years experience",
        "skills": ["Python", "FastAPI", "PostgreSQL"],
        "experience": [
            {
                "company": "Tech Co",
                "title": "Backend Engineer",
                "duration": "2020-2025",
                "bullets": [
                    "Built REST APIs using FastAPI, serving 1M+ requests daily",
                    "Optimized database queries with PostgreSQL indexes, reducing latency by 50%"
                ]
            }
        ],
        "projects": [
            {
                "name": "API Gateway",
                "technologies": ["Python", "FastAPI", "Redis"],
                "bullets": [
                    "Developed API gateway handling 10K requests/sec using FastAPI and Redis"
                ]
            }
        ],
        "education": ["BS Computer Science, 2020"]
    }
    
    resume = ImprovedResumeParsed(**improved_data)
    assert resume.name == "Jane Smith"
    assert len(resume.experience) == 1
    assert len(resume.experience[0].bullets) == 2
    assert "FastAPI" in resume.experience[0].bullets[0]

def test_improved_experience_item():
    """Test ImprovedExperienceItem model"""
    exp = ImprovedExperienceItem(
        company="StartupXYZ",
        title="Full Stack Developer",
        duration="2021-2023",
        bullets=[
            "Implemented React frontend with TypeScript, improving user satisfaction by 25%",
            "Deployed microservices to AWS ECS, reducing infrastructure costs by $5K/month"
        ]
    )
    assert exp.company == "StartupXYZ"
    assert len(exp.bullets) == 2
    assert "TypeScript" in exp.bullets[0]
    assert "AWS" in exp.bullets[1]

def test_improved_project_item():
    """Test ImprovedProjectItem model"""
    project = ImprovedProjectItem(
        name="Task Manager",
        technologies=["React", "Node.js", "MongoDB"],
        bullets=[
            "Built real-time task management app with React and Socket.io, supporting 100+ concurrent users"
        ]
    )
    assert project.name == "Task Manager"
    assert "React" in project.technologies
    assert "Socket.io" in project.bullets[0]

def test_improved_resume_missing_fields():
    """Test that missing required fields fail validation"""
    incomplete_data = {
        "name": "Test User",
        # Missing: contact, skills, experience, projects, education
    }
    
    with pytest.raises(ValidationError):
        ImprovedResumeParsed(**incomplete_data)
