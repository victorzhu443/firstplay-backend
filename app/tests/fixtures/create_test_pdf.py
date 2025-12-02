"""
Script to create a simple test PDF for testing purposes.
Run this once to generate sample_resume.pdf
"""
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

def create_test_pdf():
    filename = "tests/fixtures/sample_resume.pdf"
    c = canvas.Canvas(filename, pagesize=letter)
    
    # Add test content
    c.drawString(100, 750, "JOHN DOE")
    c.drawString(100, 730, "Software Engineer")
    c.drawString(100, 710, "")
    c.drawString(100, 690, "SKILLS")
    c.drawString(100, 670, "Python, JavaScript, React, FastAPI, SQL")
    c.drawString(100, 650, "")
    c.drawString(100, 630, "EXPERIENCE")
    c.drawString(100, 610, "Software Developer at Tech Company")
    c.drawString(100, 590, "Built web applications using modern frameworks")
    
    c.save()
    print(f"✓ Created {filename}")

if __name__ == "__main__":
    create_test_pdf()