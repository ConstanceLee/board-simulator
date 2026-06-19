#!/bin/bash
# --- Variables ---
PROJECT_ID=xenon-chain-465204-p0 # Target Agentspace GCP Project ID
PROJECT_NUMBER=107168505767 # Target Agentspace GCP Project Number
LOCATION=global
ASSISTANT_ID="default_assistant"
AGENT_NAME="board_simulator"
AS_APP=agentspace-sample-board-simulator
AGENT_DISPLAY_NAME="Woolworths Board Review Simulator"
REASONING_ENGINE="projects/107168505767/locations/us-east1/reasoningEngines/4445510229051834368"
AGENT_DESCRIPTION="Simulates possible Woolworths Group board views and responses to a given board paper."
TOOL_DESCRIPTION="Convert output md file to docx format, using the pre-formatted board-review template."
AUTH_ID=""
DISCOVERY_ENGINE_API_BASE_URL="https://discoveryengine.googleapis.com"

# --- Script Body ---
AUTH_TOKEN=$(gcloud auth application-default print-access-token)

# Clean up any existing unshared or legacy agent registrations
curl -s -X DELETE \
-H "Authorization: Bearer ${AUTH_TOKEN}" \
-H "X-Goog-User-Project: ${PROJECT_ID}" \
"${DISCOVERY_ENGINE_API_BASE_URL}/v1alpha/projects/${PROJECT_ID}/locations/global/collections/default_collection/engines/${AS_APP}/assistants/${ASSISTANT_ID}/agents/${AGENT_NAME}"

curl -s -X DELETE \
-H "Authorization: Bearer ${AUTH_TOKEN}" \
-H "X-Goog-User-Project: ${PROJECT_ID}" \
"${DISCOVERY_ENGINE_API_BASE_URL}/v1alpha/projects/${PROJECT_ID}/locations/global/collections/default_collection/engines/${AS_APP}/assistants/${ASSISTANT_ID}/agents/5039672664461353769"

# Create agent with explicit agentId and ALL_USERS sharing scope
curl -X POST \
-H "Authorization: Bearer ${AUTH_TOKEN}" \
-H "Content-Type: application/json" \
-H "X-Goog-User-Project: ${PROJECT_ID}" \
"${DISCOVERY_ENGINE_API_BASE_URL}/v1alpha/projects/${PROJECT_ID}/locations/global/collections/default_collection/engines/${AS_APP}/assistants/${ASSISTANT_ID}/agents?agentId=${AGENT_NAME}" \
-d @- <<EOF
{
  "name": "projects/${PROJECT_NUMBER}/locations/${LOCATION}/collections/default_collection/engines/${AS_APP}/assistants/${ASSISTANT_ID}/agents/${AGENT_NAME}",
  "displayName": "${AGENT_DISPLAY_NAME}",
  "description": "${AGENT_DESCRIPTION}",
  "sharingConfig": {
    "scope": "ALL_USERS"
  },
  "adk_agent_definition": {
    "tool_settings": {
      "tool_description": "${TOOL_DESCRIPTION}"
    },
    "provisioned_reasoning_engine": {
      "reasoning_engine": "${REASONING_ENGINE}"
    }
  }
}
EOF
