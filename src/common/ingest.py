# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Main file to ingest data into Chronicle."""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

import google.auth
from google.auth.transport import requests as Requests
from google.oauth2 import service_account

from common import env_constants
from common import utils

AUTHORIZATION_SCOPES = ["https://www.googleapis.com/auth/chronicle"]
CHRONICLE_API_URL = utils.get_env_var(env_constants.ENV_CHRONICLE_API_URL).strip().rstrip("/")
# Optional service account key. When unset, the runtime's own identity is used
# through Application Default Credentials.
SERVICE_ACCOUNT = utils.get_env_var(
    env_constants.ENV_CHRONICLE_SERVICE_ACCOUNT, required=False, is_secret=True
)
CHRONICLE_API_BASE = "googleapis.com"
API_VERSION = "v1"

# Threshold value in bytes for ingesting the logs to the Chronicle.
# A payload of maximum 0.95MB will be sent at a time to Chronicle.
# Chronicle Ingestion API allows the maximum 1MB of payload, however, we
# have kept 0.5MB as a buffer.
SIZE_THRESHOLD_BYTES = 950000

# Count of logs to check the batch size at once.
# We will check the payload size for 100 entries at once and
# ingest it into Chronicle if it exceeds more than 0.95MB.
LOG_BATCH_SIZE = 100

SERVICE_ACCOUNT_DICT = (
    utils.load_service_account(SERVICE_ACCOUNT, "Chronicle")
    if SERVICE_ACCOUNT else None
)

def _insert_api_version(url: str = CHRONICLE_API_URL):
  split = url.split(CHRONICLE_API_BASE)
  result = f"{split[0]}{CHRONICLE_API_BASE}/{API_VERSION}{split[1]}"
  return result

def initialize_http_session(
    service_account_json: Optional[Dict[Any, Any]] = None,
    scopes: Optional[Sequence[str]] = None,
) -> Requests.AuthorizedSession:
  """Initializes an authenticated session with Google Chronicle.

  Args:
    service_account_json (Optional[dict]): Service Account JSON key. When None,
      Application Default Credentials are used instead: the function's service
      account on Cloud Run, Workload Identity on GKE, or a Workload Identity
      Federation credential configuration named by
      GOOGLE_APPLICATION_CREDENTIALS elsewhere.
    scopes (Optional[Sequence[str]], optional): Required scopes. Defaults to
      None.

  Returns:
    Requests.AuthorizedSession: Authorized session object.
  """
  scopes = scopes or AUTHORIZATION_SCOPES
  if service_account_json:
    credentials = service_account.Credentials.from_service_account_info(
        service_account_json,
        scopes=scopes,
    )
  else:
    credentials, _ = google.auth.default(scopes=scopes)
  return Requests.AuthorizedSession(credentials)


def ingest(data: list[Any], log_type: str):
  """Prepare the chunk size of 0.95MB and send it to Chronicle.

  Args:
    data (list[Any]): Raw logs to send to Google Chronicle.
    log_type (str): Chronicle log type, for example: STIX
  """
  http_session = initialize_http_session(
      SERVICE_ACCOUNT_DICT, scopes=AUTHORIZATION_SCOPES
  )

  index = 0
  namespace = os.getenv(env_constants.ENV_CHRONICLE_NAMESPACE)

  def _build_log_entry(i: Any) -> Dict[str, str]:
    """Build a single Log entry in the format expected by ImportLogs.

    The Chronicle API expects raw log data as base64-encoded bytes, plus a
    logEntryTime/collectionTime per entry.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    entry = {
        "data": base64.b64encode(json.dumps(i).encode("utf-8")).decode(
            "utf-8"
        ),
        "logEntryTime": now,
        "collectionTime": now,
    }
    if namespace:
      entry["environmentNamespace"] = namespace
    return entry

  # Parse the data in the format expected by the Chronicle API's ImportLogs
  # method: {"inlineSource": {"logs": [{Log}, {Log}, ...]}}.
  parsed_data = list(map(_build_log_entry, data))

  # JSON payload to be sent to Chronicle.
  body = {"inlineSource": {"logs": []}}

  # Loop over the list of events to send to Chronicle.
  while index < len(parsed_data):
    # Chronicle API can receive a maximum of 1 MB of data in a single
    # execution. To be on a safer side, a chunk of size 0.95MB is created,
    # keeping 0.5MB as a buffer.

    # If size of 100 logs is greater than 0.95MB, we will loop over each log
    # separately. Else we will add 100 logs at a time and check if size is
    # less than 0.95MB or not.
    next_batch_of_logs = parsed_data[index : index + LOG_BATCH_SIZE]
    size_of_current_payload = sys.getsizeof(json.dumps(body))
    size_of_next_batch = sys.getsizeof(json.dumps(next_batch_of_logs))

    # The size of next 100 logs to add is greater than 0.95MB.
    if size_of_next_batch >= SIZE_THRESHOLD_BYTES:
      print(
          "Size of next 100 logs to ingest is greater than 0.95MB. Hence,"
          " looping over each log separately."
      )
      # Looping over each log separately.
      size_of_next_log = sys.getsizeof(json.dumps(parsed_data[index]))
      if size_of_current_payload + size_of_next_log <= SIZE_THRESHOLD_BYTES:
        body["inlineSource"]["logs"].append(parsed_data[index])
        index += 1
        continue

    # Adding the next 100 logs in the payload if the size of the payload is not
    # exceeding 0.95MB.
    elif size_of_current_payload + size_of_next_batch <= SIZE_THRESHOLD_BYTES:
      print("Adding a batch of 100 logs to the Ingestion API payload.")
      body["inlineSource"]["logs"].extend(next_batch_of_logs)
      index += LOG_BATCH_SIZE
      continue

    # A batch of logs is prepared for ingestion into the Chronicle.
    _send_logs_to_chronicle(http_session, body, log_type)
    body["inlineSource"]["logs"].clear()

  # If the data received to ingest is below 0.95MB, the above while loop is
  # yet to send the data to Chronicle. Hence, sending the data now.
  if body["inlineSource"]["logs"]:
    _send_logs_to_chronicle(http_session, body, log_type)


def _send_logs_to_chronicle(
    http_session: Requests.AuthorizedSession,
    body: Dict[str, Any],
    log_type: str,
):
  """Sends log entries to the Chronicle API for ingestion.

  Args:
    http_session (Requests.AuthorizedSession): Authorized session for HTTP
      requests.
    body (Dict[str, Any]): JSON payload to send to the Chronicle API's
      ImportLogs method.
    log_type (str): Chronicle log type.

  Raises:
    RuntimeError: Raises if any error occured during log ingestion.
  """
  url = _insert_api_version(CHRONICLE_API_URL) + f"/logTypes/{log_type}/logs:import"

  header = {"Content-Type": "application/json"}
  log_count = len(body["inlineSource"]["logs"])
  print(f"Attempting to push {log_count} log(s) to Chronicle.")

  # Calling the Chronicle API's ImportLogs method.
  # Reference - https://docs.cloud.google.com/chronicle/docs/reference/rest/
  #   v1/projects.locations.instances.logTypes.logs/import
  response = http_session.request("POST", url, json=body, headers=header)

  try:
    response.raise_for_status()
    # If the ImportLogs call is successful, the response body is empty.
    print(f"{log_count} log(s) pushed successfully to Chronicle.")
  except Exception as err:
    try:
      reason = response.json()
    except ValueError:
      reason = response.text
    raise RuntimeError(
        "Error occurred while pushing logs to Chronicle. "
        f"Status code {response.status_code}. Reason: {reason}"
    ) from err
