#!/bin/bash
set -e

#############################################
# Build: lambda_ingestion_function.py
#############################################

echo "📦 Building Lambda: Ingestion Function..."

rm -rf package lambda_ingestion_function.zip
mkdir -p package

if [ -f "requirements.txt" ]; then
  pip3 install --target ./package -r requirements.txt
fi

cp lambda_ingestion_function.py package/
cp -r common package/

cd package
zip -r ../lambda_ingestion_function.zip .
cd ..

rm -rf package

#############################################
# Build: lambda_ecb_ingestion.py
#############################################

echo "📦 Building Lambda: ECB Ingestion Function..."

rm -rf package lambda_ecb_ingestion.zip
mkdir -p package

if [ -f "requirements.txt" ]; then
  pip3 install --target ./package -r requirements.txt
fi

cp lambda_ecb_ingestion.py package/
cp -r common package/

cd package
zip -r ../lambda_ecb_ingestion.zip .
cd ..

rm -rf package

#############################################
# Build: lambda_fred_ingestion.py
#############################################

echo "📦 Building Lambda: FRED Ingestion Function..."

rm -rf package lambda_fred_ingestion.zip
mkdir -p package

if [ -f "requirements.txt" ]; then
  pip3 install --target ./package -r requirements.txt
fi

cp lambda_fred_ingestion.py package/
cp -r common package/

cd package
zip -r ../lambda_fred_ingestion.zip .
cd ..

rm -rf package

#############################################
# Build: lambda_validation_function.py
#############################################

echo "📦 Building Lambda: Validation Function..."

rm -rf package lambda_validation_function.zip
mkdir -p package

if [ -f "requirements_validation.txt" ]; then
  pip3 install --target ./package -r requirements_validation.txt
fi

cp lambda_validation_function.py package/

cd package
zip -r ../lambda_validation_function.zip .
cd ..

rm -rf package

echo "✅ Lambda packaging complete."
echo "Created: lambda_ingestion_function.zip, lambda_ecb_ingestion.zip, lambda_fred_ingestion.zip, lambda_validation_function.zip"
