#!/usr/bin/env python3
"""Insert **Persistence (data layer)** / **Tables:** / **Flow:** into API_DESIGN_CORE_FINAL_v1.md."""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "# Jira-like Core API Design (Final, Non-AI)"

# (tables, flow) — order matches ## 1 … ## 13 detailed sections (62 rows)
FLOWS_DOC1: list[tuple[str, str]] = [
    ("users", "`AuthController.issueToken` → `UserService.authenticate` → `UserRepository.findByUsername`"),
    ("users", "`UserController.getAll` → `UserService.findAll` → `UserRepository.findAll`"),
    ("users", "`UserController.getById` → `UserService.findById` → `UserRepository.findById`"),
    ("users", "`UserController.create` → `UserService.create` → `UserRepository.save`"),
    ("users", "`UserController.delete` → `UserService.delete` → `UserRepository.deleteById`"),
    ("spaces", "`SpaceController.getAll` → `SpaceService.listSpaces` → `SpaceRepository.findAllByDeletedAtIsNullOrderByIdAsc`"),
    ("spaces", "`SpaceController.getById` → `SpaceService.findById` → `SpaceRepository.findByIdAndDeletedAtIsNull`"),
    ("spaces", "`SpaceController.create` → `SpaceService.create` → `SpaceRepository.save`"),
    ("spaces", "`SpaceController.update` → `SpaceService.update` → `SpaceRepository.save`"),
    ("spaces", "`SpaceController.delete` → `SpaceService.delete` → `SpaceRepository.save`"),
    ("space_members", "`SpaceController.getMembers` → `SpaceService.getMembers` → `SpaceMemberRepository.findBySpaceId`"),
    ("space_members", "`SpaceController.addMember` → `SpaceService.addMember` → `SpaceMemberRepository.save`"),
    ("space_members", "`SpaceController.removeMember` → `SpaceService.removeMember` → `SpaceMemberRepository.deleteBySpaceIdAndUserId`"),
    ("space_groups", "`SpaceController.getSpaceGroups` → `SpaceService.getSpaceGroups` → `SpaceGroupRepository.findBySpaceId`"),
    ("space_groups", "`SpaceController.addGroup` → `SpaceService.addGroup` → `SpaceGroupRepository.save`"),
    ("space_groups", "`SpaceController.removeGroup` → `SpaceService.removeGroup` → `SpaceGroupRepository.deleteBySpaceIdAndGroupId`"),
    ("user_groups", "`GroupController.getAll` → `GroupService.findAll` → `UserGroupRepository.findAllByOrderByIdAsc`"),
    ("user_groups", "`GroupController.getById` → `GroupService.findById` → `UserGroupRepository.findById`"),
    ("user_groups", "`GroupController.create` → `GroupService.create` → `UserGroupRepository.save`"),
    ("user_groups", "`GroupController.update` → `GroupService.update` → `UserGroupRepository.save`"),
    ("user_groups", "`GroupController.delete` → `GroupService.delete` → `UserGroupRepository.deleteById`"),
    ("group_members", "`GroupController.getMembers` → `GroupService.getMembers` → `GroupMemberRepository.findByGroupId`"),
    ("group_members", "`GroupController.addMember` → `GroupService.addMember` → `GroupMemberRepository.save`"),
    ("group_members", "`GroupController.removeMember` → `GroupService.removeMember` → `GroupMemberRepository.deleteByGroupIdAndUserId`"),
    ("sprints", "`SprintController.getBySpace` → `SprintService.findBySpace` → `SprintRepository.findBySpaceIdOrderByStartDateAsc`"),
    ("sprints", "`SprintController.getById` → `SprintService.findById` → `SprintRepository.findById`"),
    ("sprints", "`SprintController.create` → `SprintService.create` → `SprintRepository.save`"),
    ("sprints", "`SprintController.update` → `SprintService.update` → `SprintRepository.save`"),
    ("sprints", "`SprintController.delete` → `SprintService.delete` → `SprintRepository.deleteById`"),
    ("issues", "`IssueController.getBySpace` → `IssueService.findBySpace` → `IssueRepository.findBySpaceIdOrderByIssueOrderAsc`"),
    ("issues", "`IssueController.getByKey` → `IssueService.findByKey` → `IssueRepository.findByIssueKey`"),
    ("issues", "`IssueController.create` → `IssueService.create` → `IssueRepository.save`"),
    ("issues", "`IssueController.update` → `IssueService.update` → `IssueRepository.save`"),
    ("issues", "`IssueController.delete` → `IssueService.delete` → `IssueRepository.delete`"),
    ("comments", "`CommentController.getByIssue` → `CommentService.findByIssue` → `CommentRepository.findByIssueIdOrderByCreatedAtAsc`"),
    ("comments", "`CommentController.create` → `CommentService.create` → `CommentRepository.save`"),
    ("comments", "`CommentController.update` → `CommentService.update` → `CommentRepository.save`"),
    ("comments", "`CommentController.delete` → `CommentService.delete` → `CommentRepository.deleteById`"),
    ("issue_attachments", "`IssueAttachmentController.getByIssue` → `IssueAttachmentService.findByIssue` → `IssueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc`"),
    ("issue_attachments", "`IssueAttachmentController.upload` → `IssueAttachmentService.upload` → `IssueAttachmentRepository.save`"),
    ("issue_attachments", "`IssueAttachmentController.download` → `IssueAttachmentService.download` → `IssueAttachmentRepository.findById`"),
    ("issue_attachments", "`IssueAttachmentController.delete` → `IssueAttachmentService.delete` → `IssueAttachmentRepository.delete`"),
    ("work_logs", "`WorkLogController.getByIssue` → `WorkLogService.findByIssue` → `WorkLogRepository.findByIssueIdOrderByLogDateDescCreatedAtDesc`"),
    ("work_logs", "`WorkLogController.create` → `WorkLogService.create` → `WorkLogRepository.save`"),
    ("work_logs", "`WorkLogController.update` → `WorkLogService.update` → `WorkLogRepository.save`"),
    ("work_logs", "`WorkLogController.delete` → `WorkLogService.delete` → `WorkLogRepository.deleteById`"),
    ("issue_history", "`IssueHistoryController.getByIssue` → `IssueHistoryService.findByIssue` → `IssueHistoryRepository.findByIssueIdOrderByCreatedAtDesc`"),
    ("issue_links", "`IssueLinkController.getByIssue` → `IssueLinkService.findByIssue` → `IssueLinkRepository.findBySourceIssueIdOrTargetIssueId`"),
    ("issue_links", "`IssueLinkController.create` → `IssueLinkService.create` → `IssueLinkRepository.save`"),
    ("issue_links", "`IssueLinkController.delete` → `IssueLinkService.delete` → `IssueLinkRepository.delete`"),
    ("issue_code_links", "`IssueCodeLinkController.getByIssue` → `IssueCodeLinkService.findByIssue` → `IssueCodeLinkRepository.findByIssueIdOrderByActivityDesc`"),
    ("issue_code_links", "`IssueCodeLinkController.create` → `IssueCodeLinkService.create` → `IssueCodeLinkRepository.save`"),
    ("issue_code_links", "`IssueCodeLinkController.delete` → `IssueCodeLinkService.delete` → `IssueCodeLinkRepository.delete`"),
    ("issue_code_links", "`IssueCodeLinkController.getBySpace` → `IssueCodeLinkService.findBySpace` → `IssueCodeLinkRepository.findByIssueSpaceIdOrderByActivityDesc`"),
    ("issue_code_links", "`IssueCodeLinkController.refreshSpace` → `IssueCodeLinkService.refreshSpace` → `IssueCodeLinkRepository.save`"),
    ("issue_code_links", "`IssueCodeLinkController.refreshIssue` → `IssueCodeLinkService.refreshIssue` → `IssueCodeLinkRepository.save`"),
    ("issue_code_links", "`IssueCodeLinkController.refreshOne` → `IssueCodeLinkService.refreshOne` → `IssueCodeLinkRepository.save`"),
    ("space_github_repos", "`SpaceGithubRepoController.list` → `GithubScanService.listRepos` → `SpaceGithubRepoRepository.findBySpaceIdOrderByCreatedAtAsc`"),
    ("space_github_repos", "`SpaceGithubRepoController.add` → `GithubScanService.addRepo` → `SpaceGithubRepoRepository.save`"),
    ("space_github_repos", "`SpaceGithubRepoController.bulkImport` → `GithubScanService.bulkAddReposFromAccount` → `SpaceGithubRepoRepository.save`"),
    ("space_github_repos", "`SpaceGithubRepoController.remove` → `GithubScanService.removeRepo` → `SpaceGithubRepoRepository.delete`"),
    ("issue_code_links", "`SpaceGithubRepoController.scan` → `GithubScanService.scanSpace` → `IssueCodeLinkRepository.save`"),
]

FLOWS_DOC2: list[tuple[str, str]] = [
    ("users", "`AuthController` (login) → `UserService.authenticate` → `UserRepository.findByUsername`"),
    ("users", "`AuthController.issueToken` → `UserService.authenticate` → `UserRepository.findByUsername`"),
    ("N/A", "`GET /api/auth/oauth2/status` — **no `JpaRepository`**"),
] + FLOWS_DOC1[1:]


def persistence_block(tables: str, flow: str) -> str:
    return (
        "\n\n**Persistence (data layer)**\n\n"
        f"**Tables:** `{tables}`\n"
        f"**Flow:** {flow}\n\n\n"
    )


def inject_doc1(pre: str) -> str:
    if "**Persistence (data layer)**" in pre:
        raise SystemExit("doc1 already has Persistence blocks; abort")
    it = iter(FLOWS_DOC1)

    def repl(m: re.Match[str]) -> str:
        tables, flow = next(it)
        return m.group(1) + persistence_block(tables, flow) + m.group(2)

    pat = re.compile(r"(\*\*After call:\*\*[^\n]*)\n\n(\*\*Endpoint:\*\*)", re.MULTILINE)
    out, n = pat.subn(repl, pre)
    if n != len(FLOWS_DOC1):
        raise SystemExit(f"doc1: expected {len(FLOWS_DOC1)} injections, got {n}")
    return out


def inject_doc2(post: str) -> str:
    head_re = re.compile(r"(^### \d+\.\d+ [^\n]+$)", re.MULTILINE)
    parts = head_re.split(post)
    out: list[str] = [parts[0]]
    fi = 0
    for i in range(1, len(parts), 2):
        heading = parts[i]
        body = parts[i + 1]
        if "**Persistence (data layer)**" in body:
            out.append(heading + body)
            continue
        if "**Endpoint:**" not in body:
            out.append(heading + body)
            continue
        if fi >= len(FLOWS_DOC2):
            raise SystemExit(f"doc2: extra section {heading!r}")
        tables, flow = FLOWS_DOC2[fi]
        idx = body.index("**Endpoint:**")
        body = body[:idx] + persistence_block(tables, flow) + body[idx:]
        fi += 1
        out.append(heading + body)
    if fi != len(FLOWS_DOC2):
        raise SystemExit(f"doc2: used {fi} flows, expected {len(FLOWS_DOC2)}")
    return "".join(out)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "API_DESIGN_CORE_FINAL_v1.md"
    text = path.read_text(encoding="utf-8")
    if MARKER not in text:
        raise SystemExit(f"marker not found: {MARKER!r}")
    pre, post = text.split(MARKER, 1)
    pre2 = inject_doc1(pre)
    post2 = inject_doc2(post)
    path.write_text(pre2 + MARKER + post2, encoding="utf-8")
    print("OK: wrote", path)


if __name__ == "__main__":
    main()
