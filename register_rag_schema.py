#!/usr/bin/env python3
"""
Google Vertex AI RAG Engine Schema Registration Utility
Registers the required metadata schemas (restricted, space_name, site_name, source_system)
on a given RAG Corpus to support post-import metadata tagging and CEL query-time filtering.
"""

import os
import argparse
import google.cloud.aiplatform as aiplatform
from vertexai.preview import rag

def main():
    parser = argparse.ArgumentParser(
        description="Register custom metadata schema columns on a Google Vertex AI RAG Corpus."
    )
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT"),
        help="Google Cloud Project ID (defaults to GOOGLE_CLOUD_PROJECT env var)"
    )
    parser.add_argument(
        "--location",
        default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        help="Google Cloud Location/Region (defaults to GOOGLE_CLOUD_LOCATION env var or us-central1)"
    )
    parser.add_argument(
        "--corpus-id",
        default=os.getenv("RAG_CORPUS_ID"),
        required=not os.getenv("RAG_CORPUS_ID"),
        help="The RAG Corpus ID (e.g. 6301661778598166528)"
    )
    
    args = parser.parse_args()
    
    if not args.project:
        print("[Error] Project ID must be specified via --project or GOOGLE_CLOUD_PROJECT env var.")
        return 1
        
    print("=" * 60)
    print("VERTEX AI RAG ENGINE - SCHEMA REGISTRATION UTILITY")
    print("=" * 60)
    print(f"Project ID:  {args.project}")
    print(f"Location:    {args.location}")
    print(f"Corpus ID:   {args.corpus_id}")
    print("-" * 60)
    
    # 1. Initialize SDK
    aiplatform.init(project=args.project, location=args.location)
    corpus_name = f"projects/{args.project}/locations/{args.location}/ragCorpora/{args.corpus_id}"
    
    # 2. Build Schema Columns Definitions
    requests = [
        rag.RagDataSchema(
            key="restricted",
            schema_details=rag.RagMetadataSchemaDetails(
                type="BOOLEAN",
                granularity="GRANULARITY_FILE_LEVEL"
            )
        ),
        rag.RagDataSchema(
            key="space_name",
            schema_details=rag.RagMetadataSchemaDetails(
                type="STRING",
                granularity="GRANULARITY_FILE_LEVEL"
            )
        ),
        rag.RagDataSchema(
            key="site_name",
            schema_details=rag.RagMetadataSchemaDetails(
                type="STRING",
                granularity="GRANULARITY_FILE_LEVEL"
            )
        ),
        rag.RagDataSchema(
            key="source_system",
            schema_details=rag.RagMetadataSchemaDetails(
                type="STRING",
                granularity="GRANULARITY_FILE_LEVEL"
            )
        )
    ]
    
    # 3. Create Schemas
    try:
        print("\nSending schema registration request to Vertex AI...")
        created_schemas = rag.batch_create_data_schemas(
            corpus_name=corpus_name,
            requests=requests
        )
        
        print("\n" + "=" * 60)
        print("🎉 SUCCESS! Metadata Schema Registered Successfully!")
        print("=" * 60)
        for s in created_schemas:
            print(f"  - Column Key:  {s.key}")
            print(f"    Data Type:   {s.schema_details.type}")
            print(f"    Granularity: {s.schema_details.granularity}")
            print("-" * 40)
        print("Your RAG Corpus is now fully equipped to accept custom metadata tags.")
        print("=" * 60 + "\n")
        return 0
        
    except Exception as e:
        print("\n" + "!" * 60)
        print(f"❌ Failed to register metadata schema: {e}")
        print("!" * 60 + "\n")
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
