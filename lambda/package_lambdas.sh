#!/bin/bash
set -e

#############################################
# Build: lambda_ingestion_function.py
#############################################

echo "📦 Building Lambda: Ingestion Function..."

# Clean up any existing files
rm -rf package lambda_ingestion_function.zip

# Create a temporary directory for dependencies
mkdir -p package

# Install dependencies from requirements.txt
if [ -f "requirements.txt" ]; then
  pip3 install --target ./package -r requirements.txt
fi

# Copy Lambda function to package directory
cp lambda_ingestion_function.py package/

# Create zip file for ingestion Lambda
cd package
zip -r ../lambda_ingestion_function.zip .
cd ..

# Clean up
rm -rf package

#############################################
# Build: lambda_validation_function.py
#############################################

echo "📦 Building Lambda: Validation Function..."

# Clean up any existing files
rm -rf package lambda_validation_function.zip

# Create a temporary directory for dependencies (if any)
mkdir -p package

# Install dependencies if validation function has its own requirements
if [ -f "requirements_validation.txt" ]; then
  pip3 install --target ./package -r requirements_validation.txt
fi

# Copy validation Lambda function to package directory
cp lambda_validation_function.py package/

# Create zip file for validation Lambda
cd package
zip -r ../lambda_validation_function.zip .
cd ..

# Clean up
rm -rf package

echo "✅ Lambda packaging complete."
echo "Created: lambda_ingestion_function.zip and lambda_validation_function.zip"