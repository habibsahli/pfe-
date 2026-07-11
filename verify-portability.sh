#!/bin/bash

################################################################################
# Fibre Forecast - Portability Verification Script
# 
# This script verifies that the project is properly configured for portability
# Run this to ensure everything is set up correctly before sharing with others
#
# Usage:
#   ./verify-portability.sh
#
################################################################################

# Don't exit on grep failing (returns non-zero when no match found)
set +e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

CHECKS_PASSED=0
CHECKS_FAILED=0
WARNINGS=0

print_header() {
    echo -e "\n${CYAN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}\n"
}

check_pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((CHECKS_PASSED++))
}

check_fail() {
    echo -e "${RED}✗${NC} $1"
    ((CHECKS_FAILED++))
}

check_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    ((WARNINGS++))
}

check_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

echo -e "${CYAN}"
cat << "EOF"
╔════════════════════════════════════════════════════════════╗
║       Fibre Forecast - Portability Verification          ║
║                                                            ║
║  This script verifies the project is portable across      ║
║  different machines and environments.                     ║
╚════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}\n"

# ==============================================================================
# 1. Docker Configuration Files
# ==============================================================================
print_header "CHECKING DOCKER CONFIGURATION FILES"

# Check docker-compose.yml
if [[ -f "docker-compose.yml" ]]; then
    check_pass "docker-compose.yml exists"
    
    # Check for hardcoded absolute paths
    if grep -q "/home/habib/pfe" docker-compose.yml 2>/dev/null; then
        check_fail "docker-compose.yml contains hardcoded paths /home/habib/pfe"
    else
        check_pass "docker-compose.yml uses relative paths"
    fi
    
    # Check for localhost references
    if grep -q "localhost" docker-compose.yml 2>/dev/null; then
        check_warn "docker-compose.yml contains localhost (should use service names for inter-container communication)"
    else
        check_pass "docker-compose.yml uses Docker service names"
    fi
else
    check_fail "docker-compose.yml not found"
fi

# Check .dockerignore
if [[ -f ".dockerignore" ]]; then
    check_pass ".dockerignore exists"
else
    check_fail ".dockerignore missing (will result in larger Docker images)"
fi

# Check .env.docker
if [[ -f ".env.docker" ]]; then
    check_pass ".env.docker exists"
    
    if grep "^[^#].*localhost" .env.docker 2>/dev/null | grep -v "^#" > /dev/null; then
        check_fail ".env.docker contains localhost (should use service names)"
    else
        check_pass ".env.docker properly configured for Docker"
    fi
else
    check_warn ".env.docker not found (optional but recommended)"
fi

# ==============================================================================
# 2. Test Files Portability
# ==============================================================================
print_header "CHECKING TEST FILES PORTABILITY"

TEST_FILES=(
    "test_e2e_smoke_test.py"
    "test_e2e_final_smoke_test.py"
    "test_feature_importance.py"
    "test_feature_importance_validation.py"
    "test_container_smoke_test.py"
)

for test_file in "${TEST_FILES[@]}"; do
    if [[ -f "$test_file" ]]; then
        # Check for hardcoded paths
        if grep -q "/home/habib/pfe" "$test_file" 2>/dev/null; then
            check_fail "$test_file contains hardcoded /home/habib/pfe paths"
        else
            # Check if using relative paths
            if grep -q "Path(__file__).parent" "$test_file" 2>/dev/null; then
                check_pass "$test_file uses relative paths (portable)"
            else
                check_warn "$test_file - could not verify relative paths"
            fi
        fi
    fi
done

# ==============================================================================
# 3. Backend Configuration
# ==============================================================================
print_header "CHECKING BACKEND CONFIGURATION"

if [[ -f "backend/Dockerfile" ]]; then
    check_pass "backend/Dockerfile exists"
    
    # Check if Dockerfile uses relative paths
    if grep -q "/home/habib" backend/Dockerfile 2>/dev/null; then
        check_fail "backend/Dockerfile contains hardcoded paths"
    else
        check_pass "backend/Dockerfile uses relative paths"
    fi
else
    check_fail "backend/Dockerfile not found"
fi

# ==============================================================================
# 4. Frontend Configuration
# ==============================================================================
print_header "CHECKING FRONTEND CONFIGURATION"

if [[ -f "frontend/Dockerfile" ]]; then
    check_pass "frontend/Dockerfile exists"
else
    check_fail "frontend/Dockerfile not found"
fi

if [[ -f "frontend/package.json" ]]; then
    check_pass "frontend/package.json exists"
else
    check_fail "frontend/package.json not found"
fi

# ==============================================================================
# 5. Environment Files
# ==============================================================================
print_header "CHECKING ENVIRONMENT CONFIGURATION"

if [[ -f ".env.example" ]]; then
    check_pass ".env.example exists"
else
    check_warn ".env.example missing (helpful for setup)"
fi

if [[ -f ".env" ]]; then
    check_info ".env file exists (local configuration)"
    
    if grep -q "/home/habib" .env 2>/dev/null; then
        check_warn ".env contains /home/habib paths (only an issue if shared)"
    fi
else
    check_info ".env not found (will be created from .env.example or .env.docker)"
fi

# ==============================================================================
# 6. Documentation
# ==============================================================================
print_header "CHECKING DOCUMENTATION"

DOCS=(
    "DOCKER_SETUP.md:Docker setup guide"
    "PORTABILITY_GUIDE.md:Portability guide"
    "README.md:Project README"
)

for doc_info in "${DOCS[@]}"; do
    IFS=':' read -r doc_file doc_name <<< "$doc_info"
    if [[ -f "$doc_file" ]]; then
        check_pass "$doc_file exists ($doc_name)"
    else
        check_warn "$doc_file missing ($doc_name)"
    fi
done

# ==============================================================================
# 7. Helper Scripts
# ==============================================================================
print_header "CHECKING HELPER SCRIPTS"

if [[ -f "docker-startup.sh" ]]; then
    check_pass "docker-startup.sh exists"
    if [[ -x "docker-startup.sh" ]]; then
        check_pass "docker-startup.sh is executable"
    else
        check_warn "docker-startup.sh not executable (run: chmod +x docker-startup.sh)"
    fi
else
    check_warn "docker-startup.sh missing (optional but recommended)"
fi

if [[ -f "verify-portability.sh" ]]; then
    check_pass "verify-portability.sh exists"
else
    check_warn "verify-portability.sh missing"
fi

# ==============================================================================
# 8. Project Structure
# ==============================================================================
print_header "CHECKING PROJECT STRUCTURE"

REQUIRED_DIRS=(
    "backend:Backend API"
    "frontend:Frontend UI"
    "data:Data directory"
    "docker:Docker configuration"
)

for dir_info in "${REQUIRED_DIRS[@]}"; do
    IFS=':' read -r dir_path dir_name <<< "$dir_info"
    if [[ -d "$dir_path" ]]; then
        check_pass "$dir_name ($dir_path) exists"
    else
        check_fail "$dir_name ($dir_path) missing"
    fi
done

# ==============================================================================
# 9. Python Portability
# ==============================================================================
print_header "CHECKING PYTHON PORTABILITY"

# Check for hardcoded paths in main backend (sampling)
if [[ -f "backend/app/main.py" ]]; then
    if grep -q "/home/habib" backend/app/main.py 2>/dev/null; then
        check_fail "backend/app/main.py contains hardcoded paths"
    else
        check_pass "backend/app/main.py appears portable"
    fi
fi

# ==============================================================================
# Summary
# ==============================================================================
print_header "PORTABILITY VERIFICATION SUMMARY"

TOTAL_CHECKS=$((CHECKS_PASSED + CHECKS_FAILED + WARNINGS))

echo -e "${GREEN}Passed:${NC}   $CHECKS_PASSED"
echo -e "${RED}Failed:${NC}   $CHECKS_FAILED"
echo -e "${YELLOW}Warnings:${NC} $WARNINGS"
echo -e "${BLUE}Total:${NC}    $TOTAL_CHECKS\n"

if [[ $CHECKS_FAILED -eq 0 ]]; then
    if [[ $WARNINGS -eq 0 ]]; then
        echo -e "${GREEN}✓ PROJECT IS FULLY PORTABLE!${NC}"
        echo -e "\nYour project is ready to share with friends and run on any machine."
    else
        echo -e "${GREEN}✓ PROJECT IS PORTABLE${NC} (with minor warnings)"
        echo -e "\nConsider addressing the warnings above for optimal setup."
    fi
else
    echo -e "${RED}✗ PORTABILITY ISSUES FOUND${NC}"
    echo -e "\nPlease address the failures above before sharing the project."
fi

# ==============================================================================
# Recommendations
# ==============================================================================
print_header "RECOMMENDATIONS"

if [[ $CHECKS_FAILED -gt 0 ]]; then
    echo -e "${YELLOW}Issues to fix:${NC}"
    echo "  1. Remove hardcoded /home/habib/pfe paths (use relative paths)"
    echo "  2. Replace localhost with Docker service names in docker-compose.yml"
    echo "  3. Ensure all Dockerfiles are present and valid"
fi

if [[ $WARNINGS -gt 0 ]]; then
    echo -e "${YELLOW}Recommended improvements:${NC}"
    echo "  1. Create .dockerignore if missing"
    echo "  2. Create .env.docker for Docker configuration"
    echo "  3. Make docker-startup.sh executable if present"
    echo "  4. Ensure all documentation files are present"
fi

if [[ $CHECKS_FAILED -eq 0 ]]; then
    echo -e "${GREEN}Ready to share:${NC}"
    echo "  1. Run: git push (or archive the project)"
    echo "  2. Send to your friend"
    echo "  3. They can run: docker-compose up -d (or ./docker-startup.sh)"
    echo "  4. Everything will work without any modifications!"
fi

echo ""
