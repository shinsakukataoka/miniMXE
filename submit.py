#!/usr/bin/env python3
import os, sys, subprocess, pathlib, datetime, copy

# Requires PyYAML:  pip install --user pyyaml
try:
    import yaml
except Exception as e:
    print("[ERR] PyYAML not installed. Do: pip install --user pyyaml")
    raise

def now_utc_tag():
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def expand_string(s: str, context: dict) -> str:
    if not isinstance(s, str):
        return s
    # simple placeholders
    s = s.replace("{timestamp}", context["timestamp"])
    # env-style vars like ${PWD} and ~
    s = os.path.expandvars(s)
    s = os.path.expanduser(s)
    return s

def deep_expand(obj, ctx):
    if isinstance(obj, dict):
        return {k: deep_expand(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_expand(v, ctx) for v in obj]
    if isinstance(obj, str):
        return expand_string(obj, ctx)
    return obj

def merge_llc_to_env(cfg: dict, env: dict):
    """Map llc section to the env names your scripts expect."""
    llc = cfg.get("llc") or {}
    sram = llc.get("sram") or {}
    jans = llc.get("jans") or {}

    def put(name, val):
        if val is None: return
        env[name] = str(val)

    # sizes
    put("SRAM_L3_SIZE", sram.get("size_bytes"))
    put("JANS_L3_SIZE", jans.get("size_bytes"))

    # hit cycles
    put("SRAM_L3_LAT_RD", sram.get("read_hit_cycles"))
    put("SRAM_L3_LAT_WR", sram.get("write_hit_cycles"))
    put("JANS_L3_LAT_RD", jans.get("read_hit_cycles"))
    put("JANS_L3_LAT_WR", jans.get("write_hit_cycles"))

    # energy
    es = (sram.get("energy") or {})
    ej = (jans.get("energy") or {})
    en_enabled = es.get("enabled") or ej.get("enabled")
    if en_enabled:
        put("ENABLE_LLC_ENERGY", 1)
    put("SRAM_E_READ",  es.get("e_read_hit_pJ"))
    put("SRAM_E_WRITE", es.get("e_write_hit_pJ"))
    put("SRAM_E_MISS",  es.get("e_miss_pJ"))
    put("SRAM_P_LEAK",  es.get("p_leak_mW"))
    put("JANS_E_READ",  ej.get("e_read_hit_pJ"))
    put("JANS_E_WRITE", ej.get("e_write_hit_pJ"))
    put("JANS_E_MISS",  ej.get("e_miss_pJ"))
    put("JANS_P_LEAK",  ej.get("p_leak_mW"))

def build_env(cfg: dict) -> dict:
    env = copy.deepcopy(os.environ)
    # generic env block
    for k, v in (cfg.get("env") or {}).items():
        env[k] = str(v)
    # common aliases
    if "ROI_M" in env and "ROI" not in env:
        env["ROI"] = env["ROI_M"]
    if "OUT_ROOT" in env:
        env["OUT_ROOT"] = expand_string(env["OUT_ROOT"], {"timestamp": now_utc_tag()})
    # llc mapping (for sniper)
    merge_llc_to_env(cfg, env)

    # optional: benchmarks list -> comma string for scripts that support it
    benches = cfg.get("benchmarks")
    if benches:
        env["BENCHMARKS"] = ",".join(benches)
    return env

def write_stamp_yaml(cfg_path: str, out_root: str):
    try:
        out = pathlib.Path(out_root)
        out.mkdir(parents=True, exist_ok=True)
        stamp = out / f"config_used_{now_utc_tag()}.yaml"
        stamp.write_text(pathlib.Path(cfg_path).read_text())
    except Exception as e:
        print(f"[WARN] could not write config stamp to {out_root}: {e}")

def submit(cfg_path: str):
    cfg_raw = yaml.safe_load(pathlib.Path(cfg_path).read_text())
    ctx = {"timestamp": now_utc_tag()}
    cfg = deep_expand(cfg_raw, ctx)

    script = cfg.get("script")
    if not script:
        raise SystemExit("config missing 'script' path")
    script = expand_string(script, ctx)
    if not pathlib.Path(script).exists():
        raise SystemExit(f"script not found: {script}")

    # sbatch args (optional)
    sbatch_args = cfg.get("sbatch_args") or []  # e.g., ["--job-name=myname"]
    # always export env
    if all("--export" not in a for a in sbatch_args):
        sbatch_args = ["--export=ALL"] + sbatch_args

    env = build_env(cfg)
    out_root = env.get("OUT_ROOT") or cfg.get("env", {}).get("OUT_ROOT")
    if out_root:
        write_stamp_yaml(cfg_path, out_root)

    cmd = ["sbatch", *sbatch_args, script]
    print("[INFO] submitting:", " ".join(cmd))
    # pass the constructed env so sbatch exports it
    res = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
    print(res.stdout.strip())
    if res.returncode != 0:
        print(res.stderr.strip())
        raise SystemExit(res.returncode)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <config.yaml>")
        sys.exit(2)
    submit(sys.argv[1])

