#!/bin/bash

################################################################################
# Fibre Forecast - Docker Startup Script
# 
# This script sets up and starts all Docker containers for the Fibre Forecast
# project. It includes prerequisite checking and comprehensive error handling.
#
# Usage:
#   ./docker-startup.sh              # Start all services
#   ./docker-startup.sh --build      # Rebuild images and start
#   ./docker-startup.sh --help       # Show this help message
#
################################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
MAX_WAIT_ATTEMPTS=45
WAIT_INTERVAL=2
REBUILD=false

################################################################################
# Utility Functions
################################################################################

print_header() {
    echo -e "\n${CYAN}════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}\n"
}

print_step() {
    echo -e "${BLUE}→${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

exit_error() {
    print_error "$1"
    exit 1
}

################################################################################
# Prerequisite Checking
################################################################################

check_docker() {
    print_step "Checking Docker installation..."
    
    if ! command -v docker &> /dev/null; then
        exit_error "Docker is not installed. Please install Docker: https://docs.docker.com/get-docker/"
    fi
    
    local docker_version=$(docker --version)
    print_success "Docker is installed: $docker_version"
}

check_docker_compose() {
    print_step "Checking Docker Compose installation..."
    
    if ! command -v docker compose &> /dev/null; then
        if ! command -v docker-compose &> /dev/null; then
            exit_error "Docker Compose is not installed. Please install Docker Compose: https://docs.docker.com/compose/install/"
        fi
    fi
    
    local compose_version=$(docker compose version)
    print_success "Docker Compose is installed: $compose_version"
}

check_docker_daemon() {
    print_step "Checking Docker daemon status..."
    
    if ! docker info &> /dev/null; then
        exit_error "Docker daemon is not running. Please start Docker."
    fi
    
    print_success "Docker daemon is running"
}

check_disk_space() {
    print_step "Checking available disk space..."
    
    local available=$(df /var/lib/docker 2>/dev/null | tail -1 | awk '{print $4}' || echo "0")
    local required_gb=20
    local required_kb=$((required_gb * 1024 * 1024))
    
    if [[ $available -lt $required_kb ]]; then
        print_warning "Low disk space: Only $(($available / 1024 / 1024))GB available (${required_gb}GB recommended)"
    else
        local available_gb=$(($available / 1024 / 1024))
        print_success "Sufficient disk space: ${available_gb}GB available"
    fi
}

check_memory() {
    print_step "Checking available memory..."
    
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        local available=$(free -m | awk '/^Mem:/ {print $7}')
        local required=8000  # 8GB in MB
        
        if [[ $available -lt $required ]]; then
            print_warning "Low memory: Only ${available}MB available (8GB recommended)"
        else
            local available_gb=$((available / 1024))
            print_success "Sufficient memory: ${available_gb}GB available"
        fi
    else
        print_info "Memory check skipped on this OS"
    fi
}

check_project_structure() {
    print_step "Verifying project structure..."
    
    local required_files=("docker-compose.yml" "backend/Dockerfile" "frontend/Dockerfile")
    local required_dirs=("backend" "frontend" "data" "docker")
    
    for dir in "${required_dirs[@]}"; do
        if [[ ! -d "$dir" ]]; then
            exit_error "Required directory missing: $dir"
        fi
    done
    
    for file in "${required_files[@]}"; do
        if [[ ! -f "$file" ]]; then
            exit_error "Required file missing: $file"
        fi
    done
    
    print_success "Project structure is complete"
}

run_prerequisites_check() {
    print_header "CHECKING PREREQUISITES"
    
    check_docker
    check_docker_compose
    check_docker_daemon
    check_project_structure
    check_disk_space
    check_memory
    
    print_success "All prerequisites met!"
}

################################################################################
# Docker Management
################################################################################

cleanup_stale_containers() {
    print_step "Cleaning up stale containers..."
    
    docker ps -aq --filter "name=fibre_" | xargs -r docker rm -f > /dev/null 2>&1 || true
    docker compose down --remove-orphans > /dev/null 2>&1 || true
    
    print_success "Cleanup complete"
}

build_images() {
    print_step "Building Docker images (this may take 10-15 minutes)..."
    
    if docker compose build; then
        print_success "Images built successfully"
    else
        exit_error "Failed to build Docker images"
    fi
}

start_services() {
    print_step "Starting Docker services..."
    
    if docker compose up -d; then
        print_success "Services started"
    else
        exit_error "Failed to start services"
    fi
}

wait_for_service() {
    local service=$1
    local check_command=$2
    local description=$3
    
    print_step "Waiting for $description..."
    
    for i in $(seq 1 $MAX_WAIT_ATTEMPTS); do
        if eval "$check_command" > /dev/null 2>&1; then
            print_success "$description is ready"
            return 0
        fi
        
        echo -n "."
        sleep $WAIT_INTERVAL
    done
    
    print_warning "$description did not become ready in time (may still work, check logs)"
    return 1
}

wait_for_services() {
    print_header "WAITING FOR SERVICES"
    
    # Backend API
    wait_for_service "backend" \
        "curl -sf http://localhost:8000/health" \
        "Backend API"
    
    # PostgreSQL
    wait_for_service "postgres" \
        "docker exec fibre_postgres pg_isready -U admin" \
        "PostgreSQL"
    
    # Milvus
    wait_for_service "milvus" \
        "docker exec fibre_milvus curl -f http://localhost:9091/healthz" \
        "Milvus Vector DB"
    
    # Ollama
    wait_for_service "ollama" \
        "docker exec fibre_ollama curl -f http://localhost:11434/api/tags" \
        "Ollama LLM"
    
    # MLflow
    wait_for_service "mlflow" \
        "curl -sf http://localhost:5001" \
        "MLflow"
    
    # Phoenix
    wait_for_service "phoenix" \
        "curl -sf http://localhost:6006/health" \
        "Phoenix Tracing"
    
    # Frontend
    wait_for_service "frontend" \
        "curl -sf http://localhost:3000" \
        "Frontend UI"
}

pull_ollama_models() {
    print_header "PULLING OLLAMA MODELS"
    print_info "This may take several minutes on first run..."
    
    local models=("bge-m3" "llama3.2:3b")
    
    for model in "${models[@]}"; do
        print_step "Pulling $model..."
        docker exec fibre_ollama ollama pull "$model" 2>&1 | tail -1 || print_warning "Model $model may already be cached"
    done
    
    print_success "Ollama models ready"
}

show_summary() {
    print_header "SETUP COMPLETE ✓"
    
    echo -e "${GREEN}The Fibre Forecast system is now running!${NC}"
    echo -e "\n${CYAN}Access URLs:${NC}"
    echo "  Frontend UI         → http://localhost:3000"
    echo "  Backend API         → http://localhost:8000"
    echo "  API Documentation   → http://localhost:8000/docs"
    echo "  MLflow UI           → http://localhost:5001"
    echo "  Phoenix Tracing     → http://localhost:6006"
    echo "  Milvus Attu UI      → http://localhost:8001"
    
    echo -e "\n${CYAN}Useful Commands:${NC}"
    echo "  View logs           → docker compose logs -f"
    echo "  View specific logs  → docker compose logs -f backend"
    echo "  Check status        → docker compose ps"
    echo "  Stop services       → docker compose down"
    echo "  Restart service     → docker compose restart backend"
    echo "  Access container    → docker exec -it fibre_backend /bin/bash"
    
    echo -e "\n${CYAN}Documentation:${NC}"
    echo "  Setup Guide         → See DOCKER_SETUP.md"
    echo "  Project README      → See README.md"
    
    echo -e "\n${GREEN}Ready to go! 🚀${NC}\n"
}

################################################################################
# Help Function
################################################################################

show_help() {
    cat << EOF
${CYAN}Fibre Forecast - Docker Startup Script${NC}

${CYAN}Usage:${NC}
    $0 [OPTIONS]

${CYAN}Options:${NC}
    --build         Rebuild all Docker images before starting
    --help, -h      Show this help message
    --no-checks     Skip prerequisite checks
    --clean         Remove all containers and volumes before starting

${CYAN}Examples:${NC}
    # Start services (normal mode)
    $0

    # Rebuild images and start
    $0 --build

    # Remove everything and start fresh
    $0 --clean --build

${CYAN}Documentation:${NC}
    See DOCKER_SETUP.md for detailed configuration and troubleshooting

EOF
}

################################################################################
# Main Execution
################################################################################

main() {
    print_header "FIBRE FORECAST - DOCKER STARTUP"
    
    # Parse arguments
    local skip_checks=false
    local clean_mode=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            --build)
                REBUILD=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            --no-checks)
                skip_checks=true
                shift
                ;;
            --clean)
                clean_mode=true
                shift
                ;;
            *)
                print_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done
    
    # Run prerequisite checks
    if [[ $skip_checks == false ]]; then
        run_prerequisites_check
    else
        print_warning "Prerequisite checks skipped"
    fi
    
    # Clean mode
    if [[ $clean_mode == true ]]; then
        print_header "CLEANING UP"
        print_step "Removing all containers and volumes..."
        docker compose down -v
        print_success "Cleanup complete"
        REBUILD=true
    fi
    
    # Build or start
    print_header "STARTING SERVICES"
    
    cleanup_stale_containers
    
    if [[ $REBUILD == true ]]; then
        build_images
    fi
    
    start_services
    wait_for_services
    
    # Pull Ollama models
    pull_ollama_models
    
    # Show summary
    show_summary
}

# Run main function
main "$@"
