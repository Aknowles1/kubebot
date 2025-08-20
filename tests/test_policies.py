import copy

from src.main import check_policies


def make_pod_container(overrides=None):
    base = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "t"},
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "image": "nginx:1.25.3",
                    "resources": {"requests": {"cpu": "10m"}, "limits": {"cpu": "20m"}},
                    "securityContext": {"readOnlyRootFilesystem": True},
                    "livenessProbe": {"httpGet": {"path": "/", "port": 80}},
                    "readinessProbe": {"httpGet": {"path": "/", "port": 80}},
                }
            ]
        },
    }
    if overrides:
        base = copy.deepcopy(base)
        # naive deep merge for tests
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict) and isinstance(a.get(k), dict):
                    merge(a[k], v)
                else:
                    a[k] = v
        merge(base, overrides)
    return base


def test_privileged_true_triggers_error():
    doc = make_pod_container({
        "spec": {"containers": [{"name": "c", "securityContext": {"privileged": True}}]}
    })
    errors, warnings = check_policies(doc, "pod.yaml")
    assert any("privileged is true" in e for e in errors)


def test_latest_tag_triggers_error():
    doc = make_pod_container({"spec": {"containers": [{"name": "c", "image": "nginx:latest"}]}})
    errors, _ = check_policies(doc, "pod.yaml")
    assert any("uses 'latest'" in e for e in errors)


def test_missing_resources_both_triggers_error():
    doc = make_pod_container({"spec": {"containers": [{"name": "c", "resources": {}}]}})
    errors, _ = check_policies(doc, "pod.yaml")
    assert any("missing both resources.requests and resources.limits" in e for e in errors)


def test_host_namespaces_true_triggers_error():
    doc = make_pod_container({"spec": {"hostNetwork": True, "hostPID": True, "hostIPC": True}})
    errors, _ = check_policies(doc, "pod.yaml")
    assert any("hostNetwork is true" in e for e in errors)
    assert any("hostPID is true" in e for e in errors)
    assert any("hostIPC is true" in e for e in errors)


def test_dangerous_capabilities_error():
    doc = make_pod_container({
        "spec": {
            "containers": [
                {
                    "name": "c",
                    "securityContext": {"capabilities": {"add": ["SYS_ADMIN", "NET_ADMIN"]}},
                }
            ]
        }
    })
    errors, _ = check_policies(doc, "pod.yaml")
    assert any("dangerous caps" in e for e in errors)


def test_hostpath_mount_without_readonly_error():
    doc = make_pod_container({
        "spec": {
            "volumes": [{"name": "h", "hostPath": {"path": "/etc"}}],
            "containers": [
                {"name": "c", "volumeMounts": [{"name": "h", "mountPath": "/host"}]}
            ],
        }
    })
    errors, _ = check_policies(doc, "pod.yaml")
    assert any("hostPath volumeMount 'h' should set readOnly: true" in e for e in errors)


def test_missing_security_hardening_generates_warnings():
    # Remove non-root, RO FS, seccomp, and probes
    doc = make_pod_container({
        "spec": {
            "securityContext": {},
            "containers": [
                {
                    "name": "c",
                    "securityContext": {},
                    "livenessProbe": None,
                    "readinessProbe": None,
                }
            ],
        }
    })
    # Ensure resources exist to not produce errors for this test
    c = doc["spec"]["containers"][0]
    c["resources"] = {"requests": {"cpu": "10m"}, "limits": {"cpu": "20m"}}
    errors, warnings = check_policies(doc, "pod.yaml")
    assert len(errors) == 0
    assert any("runAsNonRoot" in w for w in warnings)
    assert any("readOnlyRootFilesystem" in w for w in warnings)
    assert any("seccompProfile" in w for w in warnings)
    assert any("livenessProbe" in w for w in warnings)
    assert any("readinessProbe" in w for w in warnings)


def test_digest_is_not_latest_error():
    doc = make_pod_container({
        "spec": {"containers": [{"name": "c", "image": "nginx@sha256:deadbeef"}]}
    })
    errors, _ = check_policies(doc, "pod.yaml")
    assert not any("uses 'latest'" in e for e in errors)

