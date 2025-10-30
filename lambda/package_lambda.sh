#!/bin/bash
set -e

# Change to script directory
cd "$(dirname "$0")"

# Clean up any existing files
rm -rf package lambda.zip

# Create a temporary directory for dependencies
mkdir -p package

# Install dependencies from requirements.txt
pip3 install --target ./package -r requirements.txt

# Copy Lambda function to package directory
cp lambda_function.py package/

# Create zip file
cd package
zip -r ../lambda.zip .
cd ..

# Clean up
rm -rf package