[<img alt="Recorded Future" src="../../docs/RecordedFuture.png"  />](https://www.recordedfuture.com/)

# Kubernetes Deployment

Alternative to the Cloud Run Function deployment described in the [main README](../../README.md), for environments where serverless functions are not an option. The ingestion script runs as a Kubernetes `CronJob` instead of an HTTP function triggered by Cloud Scheduler. Everything else about the integration - the parser, the correlation rules and the dashboards - is unchanged, so follow the main README for those steps.

The script itself is not modified. `main.py` already runs to completion and exits when invoked directly, so no HTTP server or functions framework is involved.

## Choosing an overlay

Two [Kustomize](https://kubectl.docs.kubernetes.io/references/kustomize/) overlays share a common base. Pick the one that matches where the cluster runs.

| Overlay | Use when | How secrets are supplied |
| ------- | -------- | ------------------------ |
| [`overlays/gke-workload-identity`](overlays/gke-workload-identity) | The cluster is GKE | Google Secret Manager, read through Workload Identity. No key material in the cluster. |
| [`overlays/generic-kubernetes`](overlays/generic-kubernetes) | Any cluster: EKS, AKS, OpenShift, on-premises, or GKE without Workload Identity | A Kubernetes `Secret` mounted as files. The job makes no Google Cloud IAM calls. |

Both deploy the same workload into the `recorded-future` namespace:

- `CronJob/rf-risklist-ingest` - runs the ingestion daily at 02:00 UTC
- `ConfigMap/rf-risklist-ingest-config` - the non-secret configuration from `src/.env.yml.example`
- `ServiceAccount/rf-risklist-ingest`

## Prerequisites

The same credentials the Cloud Run Function deployment needs:

- A Recorded Future API token
- A Google SecOps ingestion authentication file, from the SecOps console at SIEM Settings -> Collection Agents -> Ingestion Authentication File
- Your SecOps customer ID, from SIEM Settings -> Profile

Plus a container registry the cluster can pull from, and a cluster running Kubernetes 1.27 or later (for the `timeZone` field on `CronJob`; see the comment in [base/cronjob.yaml](base/cronjob.yaml) to drop it on older clusters).

## Build the image

From the repository root:

```bash
docker build -t rf-risklist-ingest:1.0 .
docker tag rf-risklist-ingest:1.0 <YOUR_REGISTRY>/rf-risklist-ingest:1.0
docker push <YOUR_REGISTRY>/rf-risklist-ingest:1.0
```

The image is built from `src/`, including the bundled `psengine` wheel, so the build needs no access to Recorded Future artifacts. It runs as UID 1000 with a read-only root filesystem.

## Configure

[base/configmap.yaml](base/configmap.yaml) lists every setting with its default. `CHRONICLE_CUSTOMER_ID` is required; review `CHRONICLE_REGION`, the four `RECORDED_FUTURE_FUSION_PATH_*` values and `RECORDED_FUTURE_OFFSET`. They carry the same meaning as in `src/.env.yml.example`, documented in the [main README](../../README.md#environment-variables).

Set your own values in an overlay rather than editing the tracked files - see [Keeping your own values out of this repository](#keeping-your-own-values-out-of-this-repository).

**Keep `RECORDED_FUTURE_OFFSET` equal to the CronJob schedule.** It sets the expiration time of the ingested IoCs. If you change `schedule` in [base/cronjob.yaml](base/cronjob.yaml) to run twice daily, set the offset to `12h`, otherwise IoCs age out more slowly than they are refreshed.

## Deploy on GKE with Workload Identity

Requires a cluster with Workload Identity enabled. Autopilot clusters have it on by default; for Standard, create the cluster with `--workload-pool=$PROJECT_ID.svc.id.goog`.

```bash
export PROJECT_ID=<your-gcp-project>
export LOCATION=us-central1
export CLUSTER=rf-secops
export IMAGE=$LOCATION-docker.pkg.dev/$PROJECT_ID/recorded-future/rf-risklist-ingest:1.0

gcloud config set project $PROJECT_ID
gcloud services enable container.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com

# Registry, then build and push from the repository root
gcloud artifacts repositories create recorded-future \
  --repository-format=docker --location=$LOCATION
gcloud auth configure-docker $LOCATION-docker.pkg.dev
docker build -t $IMAGE .
docker push $IMAGE

# Secrets, both from files to avoid shell quoting
gcloud secrets create recorded-future-api-token \
  --data-file=<path/to/rf-token.txt>
gcloud secrets create chronicle-ingestion-auth \
  --data-file=<path/to/ingestion-auth.json>

# Cluster
gcloud container clusters create-auto $CLUSTER --location=$LOCATION
gcloud container clusters get-credentials $CLUSTER --location=$LOCATION

# Google service account, granted access to just those two secrets
gcloud iam service-accounts create rf-ingest \
  --display-name="Recorded Future risklist ingestion"
for SECRET in recorded-future-api-token chronicle-ingestion-auth; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:rf-ingest@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done

# Let the Kubernetes service account impersonate it
gcloud iam service-accounts add-iam-policy-binding \
  rf-ingest@$PROJECT_ID.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:$PROJECT_ID.svc.id.goog[recorded-future/rf-risklist-ingest]"
```

Then add an overlay carrying your project's values, as described in [Keeping your own values out of this repository](#keeping-your-own-values-out-of-this-repository), and apply it:

```bash
kubectl apply -k deploy/kubernetes/overlays/local/gke
```

## Deploy on any other Kubernetes cluster

Add an overlay carrying your image reference and customer ID, as described in [Keeping your own values out of this repository](#keeping-your-own-values-out-of-this-repository), then apply it. The base creates the `recorded-future` namespace:

```bash
kubectl apply -k deploy/kubernetes/overlays/local
```

Then create the Secret. The key names must match those in [overlays/generic-kubernetes/cronjob.yaml](overlays/generic-kubernetes/cronjob.yaml):

```
kubectl create secret generic rf-risklist-ingest-secrets --namespace recorded-future --from-file=rf-api-token=<path/to/rf-token.txt> --from-file=chronicle-ingestion-auth.json=<path/to/ingestion-auth.json>
```

This overlay points `RECORDED_FUTURE_SECRET_FILE` and `CHRONICLE_SERVICE_ACCOUNT_FILE` at the mounted files. Any secret in this integration can be supplied that way: set `<VARIABLE>_FILE` and the script reads the file instead of calling Google Secret Manager. Trailing newlines are stripped.

If you use an external secrets operator, point it at the same `rf-risklist-ingest-secrets` Secret name and keys, and nothing else changes.

## Keeping your own values out of this repository

The tracked manifests carry empty or placeholder values on purpose. Rather than editing them and having to remember to undo it, add an overlay of your own that holds the values for your environment. `.gitignore` already excludes `deploy/kubernetes/overlays/local/` for exactly this.

```yaml
# deploy/kubernetes/overlays/local/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: recorded-future
resources:
  - ../generic-kubernetes
images:
  - name: registry.example.com/recorded-future/rf-risklist-ingest
    newName: <YOUR_REGISTRY>/rf-risklist-ingest
    newTag: "1.0"
patches:
  - path: configmap.yaml
```

```yaml
# deploy/kubernetes/overlays/local/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: rf-risklist-ingest-config
data:
  CHRONICLE_CUSTOMER_ID: "<your customer ID>"
```

Then deploy with `kubectl apply -k deploy/kubernetes/overlays/local`.

For GKE, point the overlay at the Workload Identity base instead:

```yaml
# deploy/kubernetes/overlays/local/gke/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: recorded-future
resources:
  - ../../gke-workload-identity
images:
  - name: LOCATION-docker.pkg.dev/PROJECT_ID/recorded-future/rf-risklist-ingest
    newName: <LOCATION>-docker.pkg.dev/<PROJECT_ID>/recorded-future/rf-risklist-ingest
    newTag: "1.0"
patches:
  - path: serviceaccount.yaml
  - path: configmap.yaml
```

Alongside it, a `serviceaccount.yaml` setting `iam.gke.io/gcp-service-account` to your Google service account, and a `configmap.yaml` setting `CHRONICLE_CUSTOMER_ID` plus the `RECORDED_FUTURE_SECRET` and `CHRONICLE_SERVICE_ACCOUNT` Secret Manager paths for your project.

For anything beyond a test, prefer the same idea one level up: keep that overlay in your own private repository and consume this one as a remote base, so your values live under your own change control.

```yaml
resources:
  - github.com/RecordedFuture-ProfessionalServices/gsecops-siem-integration/deploy/kubernetes/overlays/generic-kubernetes?ref=main
```

### Testing against a local cluster

The same overlay works against Docker Desktop's built-in Kubernetes, kind, or minikube, with one convenience: these clusters can use an image straight from your local Docker daemon, so no registry is involved. Point `newName` at the local tag instead:

```yaml
images:
  - name: registry.example.com/recorded-future/rf-risklist-ingest
    newName: rf-risklist-ingest
    newTag: "1.0"
```

The base sets `imagePullPolicy: IfNotPresent`, so the local image is used as-is and never pulled.

## Verify

Trigger a run immediately rather than waiting for the schedule:

```bash
kubectl -n recorded-future create job rf-ingest-manual --from=cronjob/rf-risklist-ingest
kubectl -n recorded-future logs -f job/rf-ingest-manual
```

A successful run works through the four IoC types in turn:

```
{'domain': 'default', 'ip': 'default', 'hash': 'default', 'url': 'default'}
Ingesting domain
Adding a batch of 100 logs to the Ingestion API payload.
Attempting to push 200 log(s) to Chronicle.
200 log(s) pushed successfully to Chronicle.
```

The API caps a default risklist at 100,000 entries, so use custom fusion lists to go beyond the cap.

A `Complete` Job does not by itself prove all four risklists were ingested, so check the log:

```bash
kubectl -n recorded-future logs job/rf-ingest-manual > run.log
grep -c '^Ingesting' run.log          # expect 4
grep -E 'cannot be ingested|Error ingesting' run.log
```

Then confirm the data landed in SecOps using the **Recorded Future Data Ingestion** dashboard from the [`dashboards`](../../dashboards) directory, which shows ingestion volume per IoC type. Compare the per-type counts against the totals in the pod log.

A run's entities can also be searched directly, using whatever `CHRONICLE_NAMESPACE` you set:

```
graph.entity.namespace = "RAW_TELEMETRY"
```

`graph.metadata.collected_timestamp` is the ingestion time, and `graph.metadata.interval` spans `RECORDED_FUTURE_OFFSET` plus one hour.

Entity graph population can lag ingestion by several minutes to hours. Ingestion metrics appear much sooner, so check the dashboard before concluding a run did not land.

Clean up the manual run when you are done:

```bash
kubectl -n recorded-future delete job rf-ingest-manual
```

## Operational notes

**Resources.** The job requests 1 CPU and 3Gi of memory. The script holds a full risklist in memory while converting it for ingestion, which is why the Cloud Run Function deployment asks for 2GB; 3Gi leaves headroom as the default risklists grow. If a run is `OOMKilled`, raise both the request and the limit rather than only the limit.

**Runtime.** `activeDeadlineSeconds` is 3600, matching the function's timeout. `concurrencyPolicy: Forbid` prevents a second run from starting while one is still going.

**Failure behavior.** The two failure modes are not symmetric, which changes how you alert on them:

- A risklist that cannot be **fetched** from Recorded Future - bad token, wrong fusion path, API error - raises out of `main()`. The Job fails with a non-zero exit and `backoffLimit` retries it once, so this is visible in Job status. Note that the remaining IoC types are never attempted: one bad fusion path stops all four.
- A batch that cannot be **pushed** to SecOps is caught, logged as `Error ingesting <type> for list <name>`, and the run continues to the next IoC type, finishing with exit 0. This is *not* visible in Job status.

So alert on Job failure for the first case, and on `Error ingesting` in the pod logs or on ingestion volume in the SecOps dashboard for the second.

The `except Exception` guard around the fetch in `src/main.py` looks like it should turn the first case into a logged warning, but it does not: `fetch_risklist` returns a generator, so no HTTP request happens until the rows are consumed, which occurs outside that `try` block.

**Scheduling a subset of risklists.** To ingest IoC types on different schedules, deploy the overlay more than once with different names and set the fusion paths you want to skip to a list that is empty for that run. There is currently no single environment variable that disables an IoC type.