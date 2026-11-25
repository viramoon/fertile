"""Render a read-only release preview with the closing issues recorded by GitHub."""
import json
import re
import subprocess
import sys
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def gitlink(ref, component):
    row = run("git", "ls-tree", ref, "--", component)
    if not row:
        return None
    mode, kind, sha, name = row.split()
    if (mode, kind, name) != ("160000", "commit", component):
        raise ValueError(f"{ref}:{component} is not the expected gitlink")
    return sha


def associated_pulls(component, commits):
    found = {}
    for sha in commits:
        endpoint = f"repos/viramoon/{component}/commits/{sha}/pulls?per_page=100"
        rows = run("gh", "api", "--paginate", endpoint, "--jq", ".[] | @json")
        for row in rows.splitlines():
            pr = json.loads(row)
            if pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == f"viramoon/{component}":
                found[pr["number"]] = pr
    return [found[number] for number in sorted(found)]


def closing_issues(component, number):
    query = """query($repo:String!,$number:Int!,$endCursor:String) {
      repository(owner:"viramoon",name:$repo) {
        pullRequest(number:$number) {closingIssuesReferences(first:100,after:$endCursor) {
          nodes {url} pageInfo {hasNextPage endCursor}
        }}
      }
    }"""
    return run("gh", "api", "graphql", "--paginate", "-f", f"query={query}", "-f", f"repo={component}",
               "-F", f"number={number}", "--jq",
               ".data.repository.pullRequest.closingIssuesReferences.nodes[].url").splitlines()


def preview():
    if run("git", "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("fetch the full fertile history and release tags before preview")
    version = run("git", "show", "HEAD:VERSION")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("invalid committed VERSION")
    tags = run("git", "tag", "--merged", "HEAD", "--sort=-version:refname").splitlines()
    baseline = next((tag for tag in tags if re.fullmatch(r"v\d+\.\d+\.\d+", tag)), None)
    lines = [f"## fertile {version}（预览）", "", f"总仓库提交 `{run('git','rev-parse','HEAD')}`",
             f"比较基线 {baseline or '首次版本，包含所锁定组件的完整历史'}", ""]
    for component in ("finance", "web", "frontend"):
        target = gitlink("HEAD", component)
        if not target:
            raise ValueError(f"missing gitlink: {component}")
        if not Path(component, ".git").exists() or run("git", "-C", component, "rev-parse", "--is-shallow-repository") != "false":
            raise ValueError(f"fetch the full {component} history before preview")
        previous = gitlink(baseline, component) if baseline else None
        if previous:
            subprocess.run(["git", "-C", component, "merge-base", "--is-ancestor", previous, target], check=True)
        commits = run("git", "-C", component, "rev-list", f"{previous}..{target}" if previous else target).splitlines()
        lines.append(f"### {component} · `{target}`")
        lines.append("")
        pulls = associated_pulls(component, commits)
        for pr in pulls:
            issues = closing_issues(component, pr["number"])
            state = "merged" if pr["merged_at"] else pr["state"]
            lines.append(f"- {pr['html_url']} ({state}) · " + (", ".join(issues) if issues else "无关联关闭 issue"))
        if not pulls:
            lines.append(f"- {len(commits)} 个新增提交，GitHub 未返回关联 PR。")
        lines.append("")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        preview()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
