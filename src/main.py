import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple

from packaging.version import Version
import yaml
import requests


def debug(msg: str):
    print(f"[KubePolicy] {msg}")


def get_env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def get_changed_files(event: Dict[str, Any]) -> List[str]:
    # Prefer PR base ref diff using git
    repo_root = Path.cwd()
    def run(cmd: List[str]) -> Tuple[int, str, str]:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = p.communicate()
        return p.returncode, out, err

    files: List[str] = []
    if event.get("pull_request"):
        base_ref = event["pull_request"]["base"]["ref"]
        head_sha = event["pull_request"]["head"]["sha"]
        # Best effort: fetch base ref so we can diff locally
        rc, _, _ = run(["git", "fetch", "origin", base_ref, "--depth", "1"])
        if rc != 0:
            debug("git fetch base ref failed; trying without fetch")
        # Try diff with origin/base...HEAD
        rc, out, err = run(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"])
        if rc == 0 and out.strip():
            files = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            # Fallback: diff against merge-base of base.sha and HEAD, if base sha available
            base_sha = event["pull_request"]["base"]["sha"]
            rc, out, err = run(["git", "merge-base", base_sha, "HEAD"])
            if rc == 0:
                mb = out.strip()
                rc, out, err = run(["git", "diff", "--name-only", f"{mb}...HEAD"])
                if rc == 0:
                    files = [line.strip() for line in out.splitlines() if line.strip()]
            if not files:
                if get_env_bool("KPB_NO_FALLBACK_ALL", False):
                    debug("Diff failed; KPB_NO_FALLBACK_ALL=true, skipping repo-wide scan")
                    files = []
                else:
                    debug("Falling back to all repo files due to diff failure")
                    # As a last resort, scan all files (could be noisy)
                    files = [str(p) for p in repo_root.rglob("*") if p.is_file()]
    else:
        # Non-PR events: changed files in last commit range
        rc, out, err = run(["git", "diff", "--name-only", "HEAD^...HEAD"])
        if rc == 0 and out.strip():
            files = [line.strip() for line in out.splitlines() if line.strip()]
        else:
            files = [str(p) for p in Path.cwd().rglob("*") if p.is_file()]
    return files


def compile_globs(value: str) -> List[str]:
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    return parts


def match_any(path: str, patterns: List[str]) -> bool:
    from fnmatch import fnmatch
    if not patterns:
        return False
    # Normalize to posix style
    pp = path.replace(os.sep, "/")
    for pat in patterns:
        if fnmatch(pp, pat):
            return True
    return False


def is_k8s_yaml_file(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".yaml") or lower.endswith(".yml")


def load_yaml_documents(path: Path) -> List[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            content = f.read()
        docs = list(yaml.safe_load_all(content))
        return [d for d in docs if isinstance(d, dict)]
    except Exception as e:
        debug(f"Failed to parse YAML {path}: {e}")
        return []


# --- Location-aware YAML utilities ---
def _compose_yaml_documents_with_marks(content: str):
    try:
        return list(yaml.compose_all(content))
    except Exception:
        return []


def _build_line_index(node, base_path=None, out=None):
    """
    Build a mapping from a tuple path (keys and indices) to (line, col).
    Records the position of mapping keys, sequence items, and scalar values.
    """
    if out is None:
        out = {}
    if base_path is None:
        base_path = ()

    from yaml.nodes import MappingNode, SequenceNode, ScalarNode

    if isinstance(node, MappingNode):
        # Record the mapping itself position
        out[base_path] = (node.start_mark.line + 1, node.start_mark.column + 1)
        for key_node, val_node in node.value:
            # Only handle scalar keys
            if isinstance(key_node, ScalarNode):
                key = key_node.value
                key_path = base_path + (key,)
                out[key_path] = (key_node.start_mark.line + 1, key_node.start_mark.column + 1)
                _build_line_index(val_node, key_path, out)
            else:
                _build_line_index(val_node, base_path, out)
    elif isinstance(node, SequenceNode):
        out[base_path] = (node.start_mark.line + 1, node.start_mark.column + 1)
        for idx, item in enumerate(node.value):
            item_path = base_path + (idx,)
            out[item_path] = (item.start_mark.line + 1, item.start_mark.column + 1)
            _build_line_index(item, item_path, out)
    else:
        # Scalar node
        out[base_path] = (node.start_mark.line + 1, node.start_mark.column + 1)
    return out


def _lookup_line(line_index: Dict[tuple, Tuple[int, int]], path: Tuple[Any, ...], fallback: Tuple[int, int] = (1, 1)) -> Tuple[int, int]:
    # Try exact path; if missing, walk back to ancestors
    p = path
    while p:
        if p in line_index:
            return line_index[p]
        p = p[:-1]
    return fallback


def _resolve_pod_spec_and_path(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], Tuple[Any, ...]]:
    kind = (doc.get("kind") or "").lower()
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        return {}, ()
    if kind == "pod":
        return spec, ("spec",)
    # Workload kinds
    if isinstance(spec.get("template"), dict) and isinstance(spec["template"].get("spec"), dict):
        return spec["template"]["spec"], ("spec", "template", "spec")
    if kind == "job":
        tmpl = (spec.get("template") or {})
        if isinstance(tmpl.get("spec"), dict):
            return tmpl["spec"], ("spec", "template", "spec")
    if kind == "cronjob":
        jt = (spec.get("jobTemplate") or {}).get("spec") or {}
        if isinstance(jt, dict):
            t = (jt.get("template") or {}).get("spec") or {}
            if isinstance(t, dict):
                return t, ("spec", "jobTemplate", "spec", "template", "spec")
    return {}, ()



def print_annotation(level: str, file: str, message: str, line: int = 1, col: int = 1):
    # level: error or warning
    safe_msg = message.replace("\n", " ")
    # Clamp to minimum of 1 to avoid invalid annotations
    line = int(line) if isinstance(line, int) and line > 0 else 1
    col = int(col) if isinstance(col, int) and col > 0 else 1
    print(f"::{level} file={file},line={line},col={col}::{safe_msg}")


def check_policies_with_locations(doc: Dict[str, Any], line_index: Dict[tuple, Tuple[int, int]]):
    """
    Returns (errors_with_lines, warnings_with_lines) where each list contains
    tuples of (message, (line, col)).
    """
    errors = []  # List[Tuple[str, Tuple[int,int]]]
    warnings = []

    pod_spec, pod_spec_path = _resolve_pod_spec_and_path(doc)
    if not isinstance(pod_spec, dict) or not pod_spec_path:
        return errors, warnings

    # hostNetwork/hostPID/hostIPC
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if bool(pod_spec.get(flag)):
            path = pod_spec_path + (flag,)
            loc = _lookup_line(line_index, path)
            errors.append((f"{flag} is true", loc))

    containers = list_containers(pod_spec)
    pod_sc = pod_spec.get("securityContext") or {}

    vol_index = build_volume_index(pod_spec)

    # Helper to find path to containers list
    containers_path = None
    for key in ("containers", "initContainers", "ephemeralContainers"):
        if isinstance(pod_spec.get(key), list):
            containers_path = pod_spec_path + (key,)
            break

    for idx, c in enumerate(containers):
        name = c.get("name") or "<unnamed>"
        image = c.get("image") or ""
        sc = c.get("securityContext") or {}
        c_path = (containers_path + (idx,)) if containers_path is not None else pod_spec_path

        # privileged
        if isinstance(sc, dict) and bool(sc.get("privileged")):
            path = c_path + ("securityContext", "privileged")
            loc = _lookup_line(line_index, path)
            errors.append((f"container '{name}': securityContext.privileged is true", loc))

        # image latest/no tag
        if isinstance(image, str) and image:
            if image_uses_latest_or_no_tag(image):
                path = c_path + ("image",)
                loc = _lookup_line(line_index, path)
                errors.append((f"container '{name}': image '{image}' uses 'latest' or has no tag", loc))

        # missing resources
        res = c.get("resources") or {}
        reqs = (res.get("requests") or {}) if isinstance(res, dict) else {}
        lims = (res.get("limits") or {}) if isinstance(res, dict) else {}
        if not reqs and not lims:
            # Point to resources key if present, else container start
            path = c_path + (("resources",) if "resources" in c else tuple())
            loc = _lookup_line(line_index, path)
            errors.append((f"container '{name}': missing both resources.requests and resources.limits", loc))

        # dangerous capabilities
        dangerous = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "DAC_READ_SEARCH"}
        caps = ((sc.get("capabilities") or {}).get("add") or []) if isinstance(sc, dict) else []
        found = []
        if isinstance(caps, list):
            for cap in caps:
                if not isinstance(cap, str):
                    continue
                n = normalize_cap(cap)
                if n in dangerous:
                    found.append(n)
        if found:
            path = c_path + ("securityContext", "capabilities", "add")
            loc = _lookup_line(line_index, path)
            errors.append((f"container '{name}': capabilities.add includes dangerous caps: {', '.join(sorted(set(found)))}", loc))

        # hostPath volume mounts without readOnly: true
        vmounts = c.get("volumeMounts") or []
        if isinstance(vmounts, list):
            for m_idx, m in enumerate(vmounts):
                if not isinstance(m, dict):
                    continue
                vname = m.get("name")
                if not vname or vname not in vol_index:
                    continue
                vol = vol_index[vname]
                if isinstance(vol, dict) and isinstance(vol.get("hostPath"), dict):
                    if not bool(m.get("readOnly")):
                        path = c_path + ("volumeMounts", m_idx, "readOnly")
                        # If readOnly key missing, fall back to mount entry
                        loc = _lookup_line(line_index, path, _lookup_line(line_index, c_path + ("volumeMounts", m_idx)))
                        errors.append((f"container '{name}': hostPath volumeMount '{vname}' should set readOnly: true", loc))

        # Warnings
        pod_run_as_nonroot = bool(isinstance(pod_sc, dict) and pod_sc.get("runAsNonRoot") is True)
        c_run_as_nonroot = bool(isinstance(sc, dict) and sc.get("runAsNonRoot") is True)
        if not (pod_run_as_nonroot or c_run_as_nonroot):
            loc = _lookup_line(line_index, c_path + ("securityContext", "runAsNonRoot"), _lookup_line(line_index, c_path))
            warnings.append((f"container '{name}': missing runAsNonRoot: true (pod or container securityContext)", loc))

        if not (isinstance(sc, dict) and sc.get("readOnlyRootFilesystem") is True):
            loc = _lookup_line(line_index, c_path + ("securityContext", "readOnlyRootFilesystem"), _lookup_line(line_index, c_path))
            warnings.append((f"container '{name}': missing readOnlyRootFilesystem: true", loc))

        def has_runtime_default(obj: Dict[str, Any]) -> bool:
            if not isinstance(obj, dict):
                return False
            sp = obj.get("seccompProfile")
            if not isinstance(sp, dict):
                return False
            return (sp.get("type") == "RuntimeDefault")

        if not (has_runtime_default(pod_sc) or has_runtime_default(sc)):
            # Prefer pod-level seccomp location if exists, else container
            loc = _lookup_line(line_index, pod_spec_path + ("securityContext", "seccompProfile"), _lookup_line(line_index, c_path + ("securityContext", "seccompProfile"), _lookup_line(line_index, c_path)))
            warnings.append((f"container '{name}': missing seccompProfile.type: RuntimeDefault (pod or container)", loc))

        if not c.get("livenessProbe"):
            loc = _lookup_line(line_index, c_path + ("livenessProbe",), _lookup_line(line_index, c_path))
            warnings.append((f"container '{name}': missing livenessProbe", loc))
        if not c.get("readinessProbe"):
            loc = _lookup_line(line_index, c_path + ("readinessProbe",), _lookup_line(line_index, c_path))
            warnings.append((f"container '{name}': missing readinessProbe", loc))

    return errors, warnings


def normalize_cap(cap: str) -> str:
    c = cap.strip().upper()
    if c.startswith("CAP_"):
        c = c[4:]
    return c


def image_uses_latest_or_no_tag(image: str) -> bool:
    # Accept digests as pinned (image@sha256:...)
    if "@" in image:
        return False
    # Extract last segment
    last = image.split("/")[-1]
    if ":" not in last:
        return True
    tag = last.split(":", 1)[1]
    return tag == "latest"


def list_containers(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    containers = []
    for key in ("containers", "initContainers", "ephemeralContainers"):
        arr = spec.get(key) or []
        if isinstance(arr, list):
            for c in arr:
                if isinstance(c, dict):
                    c = {**c, "__kind": key}
                    containers.append(c)
    return containers


def build_volume_index(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    idx = {}
    vols = spec.get("volumes") or []
    if isinstance(vols, list):
        for v in vols:
            if isinstance(v, dict) and "name" in v:
                idx[v["name"]] = v
    return idx


def check_policies(doc: Dict[str, Any], file_path: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    kind = (doc.get("kind") or "").lower()
    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        return errors, warnings

    # Resolve pod template spec for workload kinds
    pod_spec = None
    template = spec.get("template") if isinstance(spec, dict) else None
    if isinstance(template, dict) and isinstance(template.get("spec"), dict):
        pod_spec = template["spec"]
    elif kind in {"pod"}:
        pod_spec = spec
    else:
        # Try common job/cronjob path
        if kind in {"job"} and isinstance(spec.get("template"), dict):
            pod_spec = spec["template"].get("spec") or {}
        elif kind in {"cronjob"} and isinstance(spec.get("jobTemplate"), dict):
            jt = spec["jobTemplate"].get("spec") or {}
            pod_spec = (jt.get("template") or {}).get("spec") or {}

    if not isinstance(pod_spec, dict):
        return errors, warnings

    # Error: hostNetwork/hostPID/hostIPC true
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if bool(pod_spec.get(flag)):
            errors.append(f"{flag} is true")

    # Containers
    containers = list_containers(pod_spec)
    pod_sc = pod_spec.get("securityContext") or {}

    # Volumes
    vol_index = build_volume_index(pod_spec)

    for c in containers:
        name = c.get("name") or "<unnamed>"
        image = c.get("image") or ""
        sc = c.get("securityContext") or {}

        # Error: securityContext.privileged: true
        if isinstance(sc, dict) and bool(sc.get("privileged")):
            errors.append(f"container '{name}': securityContext.privileged is true")

        # Error: image latest or no tag
        if isinstance(image, str) and image:
            if image_uses_latest_or_no_tag(image):
                errors.append(f"container '{name}': image '{image}' uses 'latest' or has no tag")

        # Error: missing both resources.requests and resources.limits
        res = c.get("resources") or {}
        reqs = (res.get("requests") or {}) if isinstance(res, dict) else {}
        lims = (res.get("limits") or {}) if isinstance(res, dict) else {}
        if not reqs and not lims:
            errors.append(f"container '{name}': missing both resources.requests and resources.limits")

        # Error: dangerous capabilities added
        dangerous = {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "DAC_READ_SEARCH"}
        caps = ((sc.get("capabilities") or {}).get("add") or []) if isinstance(sc, dict) else []
        found = []
        if isinstance(caps, list):
            for cap in caps:
                if not isinstance(cap, str):
                    continue
                n = normalize_cap(cap)
                if n in dangerous:
                    found.append(n)
        if found:
            errors.append(f"container '{name}': capabilities.add includes dangerous caps: {', '.join(sorted(set(found)))}")

        # Error: hostPath volumes without readOnly: true on mounts
        vmounts = c.get("volumeMounts") or []
        if isinstance(vmounts, list):
            for m in vmounts:
                if not isinstance(m, dict):
                    continue
                vname = m.get("name")
                if not vname or vname not in vol_index:
                    continue
                vol = vol_index[vname]
                if isinstance(vol, dict) and isinstance(vol.get("hostPath"), dict):
                    if not bool(m.get("readOnly")):
                        errors.append(f"container '{name}': hostPath volumeMount '{vname}' should set readOnly: true")

        # Warning: Missing runAsNonRoot: true (pod or container)
        pod_run_as_nonroot = bool(isinstance(pod_sc, dict) and pod_sc.get("runAsNonRoot") is True)
        c_run_as_nonroot = bool(isinstance(sc, dict) and sc.get("runAsNonRoot") is True)
        if not (pod_run_as_nonroot or c_run_as_nonroot):
            warnings.append(f"container '{name}': missing runAsNonRoot: true (pod or container securityContext)")

        # Warning: Missing readOnlyRootFilesystem: true
        if not (isinstance(sc, dict) and sc.get("readOnlyRootFilesystem") is True):
            warnings.append(f"container '{name}': missing readOnlyRootFilesystem: true")

        # Warning: Missing seccompProfile: RuntimeDefault (pod or container)
        def has_runtime_default(obj: Dict[str, Any]) -> bool:
            if not isinstance(obj, dict):
                return False
            sp = obj.get("seccompProfile")
            if not isinstance(sp, dict):
                return False
            return (sp.get("type") == "RuntimeDefault")

        if not (has_runtime_default(pod_sc) or has_runtime_default(sc)):
            warnings.append(f"container '{name}': missing seccompProfile.type: RuntimeDefault (pod or container)")

        # Warning: Missing livenessProbe or readinessProbe
        if not c.get("livenessProbe"):
            warnings.append(f"container '{name}': missing livenessProbe")
        if not c.get("readinessProbe"):
            warnings.append(f"container '{name}': missing readinessProbe")

    return errors, warnings


def generate_suggestions() -> str:
    # Provide generic patch snippets for users to apply
    return "\n".join([
        "### Suggested YAML patches",
        "",
        "- Avoid privileged:",
        "```yaml",
        "securityContext:",
        "  privileged: false",
        "```",
        "- Disable host namespace sharing:",
        "```yaml",
        "spec:",
        "  hostNetwork: false",
        "  hostPID: false",
        "  hostIPC: false",
        "```",
        "- Pin image tags (avoid latest) and/or use digests:",
        "```yaml",
        "containers:",
        "- name: app",
        "  image: myrepo/myimage:1.2.3  # or myimage@sha256:...",
        "```",
        "- Define resources requests and limits:",
        "```yaml",
        "resources:",
        "  requests:",
        "    cpu: \"100m\"",
        "    memory: \"128Mi\"",
        "  limits:",
        "    cpu: \"500m\"",
        "    memory: \"512Mi\"",
        "```",
        "- Remove dangerous Linux capabilities:",
        "```yaml",
        "securityContext:",
        "  capabilities:",
        "    drop: [\"ALL\"]",
        "    # add only minimal required",
        "```",
        "- Mount hostPath read-only:",
        "```yaml",
        "volumes:",
        "- name: host-vol",
        "  hostPath:",
        "    path: /host/path",
        "containers:",
        "- name: app",
        "  volumeMounts:",
        "  - name: host-vol",
        "    mountPath: /mount/path",
        "    readOnly: true",
        "```",
        "- Enforce non-root, read-only FS, and seccomp:",
        "```yaml",
        "securityContext:",
        "  runAsNonRoot: true",
        "  readOnlyRootFilesystem: true",
        "  seccompProfile:",
        "    type: RuntimeDefault",
        "```",
        "- Add health probes:",
        "```yaml",
        "livenessProbe:",
        "  httpGet: { path: /healthz, port: 8080 }",
        "  initialDelaySeconds: 10",
        "  periodSeconds: 10",
        "readinessProbe:",
        "  httpGet: { path: /ready, port: 8080 }",
        "  initialDelaySeconds: 5",
        "  periodSeconds: 5",
        "```",
    ])


def build_comment(summary: Dict[str, Any]) -> str:
    lines = []
    lines.append("## KubePolicy PR Bot")
    lines.append("")
    lines.append(f"Scanned {summary['files_scanned']} file(s). Found {summary['error_count']} error(s) and {summary['warning_count']} warning(s).")
    lines.append("")
    for fpath, findings in summary["per_file"].items():
        lines.append(f"- {fpath}")
        for e in findings.get("errors", []):
            lines.append(f"  - E: {e}")
        for w in findings.get("warnings", []):
            lines.append(f"  - W: {w}")
    lines.append("")
    lines.append(generate_suggestions())
    return "\n".join(lines)


def post_pr_comment(event: Dict[str, Any], token: str, body: str) -> bool:
    if not event.get("pull_request"):
        return False
    pr = event["pull_request"]
    repo = event["repository"]["full_name"]
    owner, name = repo.split("/")
    number = pr["number"]
    url = f"https://api.github.com/repos/{owner}/{name}/issues/{number}/comments"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}",
        "User-Agent": "kubepolicy-pr-bot"
    }
    resp = requests.post(url, headers=headers, json={"body": body})
    if resp.status_code >= 200 and resp.status_code < 300:
        debug("Posted PR comment successfully")
        return True
    else:
        debug(f"Failed to post PR comment: {resp.status_code} {resp.text}")
        return False


def main() -> int:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8") as f:
            event = json.load(f)
    else:
        event = {}

    include_globs = compile_globs(os.getenv("INPUT_INCLUDE_GLOB", "**/*.yml,**/*.yaml"))
    exclude_globs = compile_globs(os.getenv("INPUT_EXCLUDE_GLOB", ""))
    severity_threshold = (os.getenv("INPUT_SEVERITY_THRESHOLD", "error") or "error").strip().lower()
    post_comment = get_env_bool("INPUT_POST_PR_COMMENT", True)
    github_token = os.getenv("INPUT_GITHUB_TOKEN", "").strip()
    json_output_path = os.getenv("KPB_JSON_OUTPUT", "").strip()

    # Local override: allow specifying file globs to scan explicitly
    override_globs = compile_globs(os.getenv("KPB_FILE_GLOBS", ""))

    candidates: List[str] = []
    if override_globs:
        import glob as _glob
        seen = set()
        for pat in override_globs:
            for path in _glob.glob(pat, recursive=True):
                if path in seen:
                    continue
                seen.add(path)
                if not is_k8s_yaml_file(path):
                    continue
                if exclude_globs and match_any(path, exclude_globs):
                    continue
                candidates.append(path)
        debug(f"Using override globs, found {len(candidates)} file(s)")
    else:
        changed = get_changed_files(event)
        for path in changed:
            if not is_k8s_yaml_file(path):
                continue
            if include_globs and not match_any(path, include_globs):
                continue
            if exclude_globs and match_any(path, exclude_globs):
                continue
            candidates.append(path)

    if not candidates:
        debug("No matching YAML files to scan.")
        return 0

    total_errors = 0
    total_warnings = 0
    per_file: Dict[str, Dict[str, List[str]]] = {}

    for path in candidates:
        p = Path(path)
        try:
            with p.open("r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            debug(f"Failed to read {path}: {e}")
            continue

        docs = [d for d in yaml.safe_load_all(content) if isinstance(d, dict)]
        nodes = _compose_yaml_documents_with_marks(content)
        file_errors: List[str] = []
        file_warnings: List[str] = []

        for idx, doc in enumerate(docs):
            line_index = {}
            if idx < len(nodes) and nodes[idx] is not None:
                try:
                    line_index = _build_line_index(nodes[idx])
                except Exception:
                    line_index = {}

            errs_with_loc, warns_with_loc = check_policies_with_locations(doc, line_index)

            # Emit annotations with line info
            for msg, (ln, col) in errs_with_loc:
                print_annotation("error", path, msg, ln, col)
            for msg, (ln, col) in warns_with_loc:
                print_annotation("warning", path, msg, ln, col)

            # Collect plain strings for summary
            file_errors.extend([msg for msg, _ in errs_with_loc])
            file_warnings.extend([msg for msg, _ in warns_with_loc])

        total_errors += len(file_errors)
        total_warnings += len(file_warnings)
        per_file[path] = {"errors": file_errors, "warnings": file_warnings}

    summary = {
        "files_scanned": len(candidates),
        "error_count": total_errors,
        "warning_count": total_warnings,
        "per_file": per_file,
    }

    # Optional JSON summary output
    if json_output_path:
        try:
            with open(json_output_path, "w", encoding="utf-8") as jf:
                json.dump(summary, jf, indent=2, sort_keys=True)
            debug(f"Wrote JSON summary to {json_output_path}")
        except Exception as e:
            debug(f"Failed to write JSON summary: {e}")

    if post_comment and event.get("pull_request"):
        comment_body = build_comment(summary)
        if github_token:
            try:
                post_pr_comment(event, github_token, comment_body)
            except Exception as e:
                debug(f"Exception posting PR comment: {e}")
        else:
            debug("No github_token provided; printing comment body to logs")
            print(comment_body)

    # Determine exit code by threshold
    if severity_threshold == "warning":
        return 1 if (total_errors + total_warnings) > 0 else 0
    else:
        return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
