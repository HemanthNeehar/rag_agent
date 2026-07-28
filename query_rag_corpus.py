#!/usr/bin/env python3
"""Standalone script to query the Vertex AI RAG Corpus directly.

This bypasses the ADK reasoning engine completely, allowing you to test vector 
retrieval, dynamic CEL filtering, and similarity scores programmatically.

Usage:
    python query_rag_corpus.py --query "What is GIT counts?"
    python query_rag_corpus.py --query "What is GIT counts?" --email "guest@example.com"
    python query_rag_corpus.py --query "What is GIT counts?" --email "nikita.rathore@lumen.com" --groups "HR,PAYROLL"
"""

import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

import vertexai
from vertexai.preview import rag

# Load environment variables
script_dir = Path(__file__).resolve().parent
load_dotenv(script_dir.parent / ".env")
load_dotenv(script_dir / ".env", override=True)

# Auth setup
service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH")
if service_account_path:
    if not os.path.isabs(service_account_path):
        service_account_path = str(script_dir / service_account_path)
    if Path(service_account_path).exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = service_account_path
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"

# Configuration
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "agent-ops-494011")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
CORPUS_ID = os.getenv("RAG_CORPUS_ID")

if not CORPUS_ID:
    print("[Error] RAG_CORPUS_ID env var is not set. Please set it in your .env file.")
    exit(1)

# Initialize Vertex AI SDK
vertexai.init(project=PROJECT_ID, location=LOCATION)
CORPUS_NAME = f"projects/{PROJECT_ID}/locations/{LOCATION}/ragCorpora/{CORPUS_ID}"


def query_corpus(query_text: str, email: str, groups: list[str], top_k: int = 5):
    """Sends retrieval query directly to the Vertex RAG API."""
    print("=" * 70)
    print(f"DIRECT CORPUS QUERY RETRIEVAL")
    print("=" * 70)
    print(f"Project ID    : {PROJECT_ID}")
    print(f"Location      : {LOCATION}")
    print(f"Corpus Name   : {CORPUS_NAME}")
    print(f"Query Text    : '{query_text}'")
    print(f"User Email    : {email}")
    print(f"User Groups   : {groups}")
    
    # 1. Dynamically construct CEL Metadata Filters
    cel_filter = None
    if email or groups:
        privileged_groups = {"HR", "PAYROLL", "LEGAL", "EXEC_BOARD"}
        user_upper_groups = {g.upper() for g in groups} if groups else set()
        user_priv_groups = user_upper_groups.intersection(privileged_groups)

        if not user_priv_groups:
            # Standard/Guest user: strict public documents only
            cel_filter = "restricted == false"
        else:
            # Privileged user: public documents OR specific group departments
            clauses = ["restricted == false"]
            for priv in user_priv_groups:
                clauses.append(f'department == "{priv.lower()}"')
            cel_filter = " || ".join(clauses)
            
    # 2. Build retrieval configuration
    retrieval_config = None
    if cel_filter:
        print(f"Active Filter : {cel_filter}")
        try:
            from vertexai.preview.rag import RagRetrievalConfig, Filter
            retrieval_config = RagRetrievalConfig(
                filter=Filter(
                    vector_distance_threshold=0.5,
                    metadata_filter=cel_filter
                )
            )
        except Exception as e:
            print(f"[Warning] Failed to construct retrieval config: {e}")

    # 3. Call the API
    try:
        if retrieval_config:
            response = rag.retrieval_query(
                rag_resources=[rag.RagResource(rag_corpus=CORPUS_NAME)],
                text=query_text,
                similarity_top_k=top_k,
                rag_retrieval_config=retrieval_config,
            )
        else:
            response = rag.retrieval_query(
                rag_resources=[rag.RagResource(rag_corpus=CORPUS_NAME)],
                text=query_text,
                similarity_top_k=top_k,
                vector_distance_threshold=0.5,
            )
            
        # 4. Parse & Display Results
        contexts = response.contexts.contexts if response.contexts else []
        print("\n" + "-" * 70)
        print(f"RESULTS RETRIEVED: {len(contexts)} Chunks")
        print("-" * 70)
        
        if not contexts:
            print("No matching document chunks found in the RAG corpus.")
            return

        for idx, ctx in enumerate(contexts, 1):
            source_uri = ctx.source_uri or "unknown"
            score = getattr(ctx, "distance", None) or getattr(ctx, "score", None) or 0.0
            print(f"\n[{idx}] SOURCE GCS URI: {source_uri}")
            print(f"    Similarity Distance : {score:.4f}")
            
            # Print text preview (first 300 chars)
            clean_text = " ".join((ctx.text or "").split())
            preview = clean_text[:300] + "..." if len(clean_text) > 300 else clean_text
            print(f"    Text Preview        : \"{preview}\"")
            print("-" * 70)

        # 5. Output the FULL untruncated text content of the nearest chunk (index 0)
        nearest_chunk = contexts[0]
        print("\n" + "=" * 70)
        print("🌟 NEAREST CHUNK FULL TEXT CONTENT (RELEVANCE RANK #1)")
        print("=" * 70)
        print(f"Source GCS URI      : {nearest_chunk.source_uri or 'unknown'}")
        print("-" * 70)
        print(nearest_chunk.text or "(Empty Content)")
        print("=" * 70 + "\n")

    except Exception as e:
        print(f"[Error] Direct RAG API query execution failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query Vertex AI RAG Corpus Directly")
    parser.add_argument("--query", type=str, required=True, help="Retrieval text query")
    parser.add_argument("--email", type=str, default="", help="User email address for CEL testing")
    parser.add_argument("--groups", type=str, default="", help="Comma-separated user groups")
    parser.add_argument("--top_k", type=int, default=5, help="Number of chunks to return")
    
    args = parser.parse_args()
    
    group_list = [g.strip() for g in args.groups.split(",") if g.strip()] if args.groups else []
    
    query_corpus(
        query_text=args.query,
        email=args.email,
        groups=group_list,
        top_k=args.top_k
    )
