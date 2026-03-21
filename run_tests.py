#!/usr/bin/env python3
"""
Resonant Client Test Runner

Run all tests:          python run_tests.py
Run fast unit tests:    python run_tests.py --unit
Run adversarial tests:  python run_tests.py --adversarial
Run with coverage:      python run_tests.py --coverage
Run specific module:    python run_tests.py --module protocol
Run and stop on first:  python run_tests.py --fail-fast
Verbose output:         python run_tests.py --verbose
"""

import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Run Resonant Client tests")
    parser.add_argument("--unit", action="store_true", help="Run only unit tests")
    parser.add_argument("--integration", action="store_true", help="Run only integration tests")
    parser.add_argument("--adversarial", action="store_true", help="Run only adversarial tests")
    parser.add_argument("--coverage", action="store_true", help="Run with coverage report")
    parser.add_argument("--module", "-m", type=str, help="Run specific module (protocol, diff_review, rag, backends)")
    parser.add_argument("--fail-fast", "-x", action="store_true", help="Stop on first failure")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--parallel", "-p", action="store_true", help="Run tests in parallel (requires pytest-xdist)")
    parser.add_argument("--no-header", action="store_true", help="Minimal output")
    args = parser.parse_args()

    cmd = [sys.executable, "-m", "pytest"]

    # Module selection
    if args.module:
        module_map = {
            "protocol": "tests/test_protocol.py",
            "diff_review": "tests/test_diff_review.py",
            "diff": "tests/test_diff_review.py",
            "rag": "tests/test_rag.py",
            "backends": "tests/test_backends.py",
            "adaptive": "tests/test_backends.py",
        }
        target = module_map.get(args.module)
        if target:
            cmd.append(target)
        else:
            print(f"Unknown module: {args.module}")
            print(f"Available: {', '.join(module_map.keys())}")
            return 1

    # Marker selection
    if args.unit:
        cmd.extend(["-m", "unit"])
    elif args.integration:
        cmd.extend(["-m", "integration"])
    elif args.adversarial:
        cmd.extend(["-m", "adversarial"])

    # Options
    if args.fail_fast:
        cmd.append("-x")

    if args.verbose:
        cmd.append("-vv")

    if args.parallel:
        cmd.extend(["-n", "auto"])

    if args.no_header:
        cmd.append("--no-header")

    # Coverage
    if args.coverage:
        cmd.extend([
            "--cov=resonant_client",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "--cov-config=pyproject.toml",
        ])

    # Print what we're running
    print(f"Running: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, cwd=str(__import__("pathlib").Path(__file__).parent))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
