# Jira-like Core API Design (Final, Frontend-Used Only)

## Table of Contents

- [Jira-like Core API Design (Final, Frontend-Used Only)](#jira-like-core-api-design-final-frontend-used-only)
  - [Overview](#overview)
  - [Conventions](#conventions)
  - [1. Auth Endpoints](#1-auth-endpoints)
  - [2. User Endpoints](#2-user-endpoints)
  - [3. Space Endpoints](#3-space-endpoints)
  - [4. Group Endpoints](#4-group-endpoints)
  - [5. Sprint Endpoints](#5-sprint-endpoints)
  - [6. Issue Endpoints](#6-issue-endpoints)
  - [7. Comment Endpoints](#7-comment-endpoints)
  - [8. Attachment Endpoints](#8-attachment-endpoints)
  - [9. Work Log Endpoints](#9-work-log-endpoints)
  - [10. Issue History Endpoints](#10-issue-history-endpoints)
  - [11. Issue Link Endpoints](#11-issue-link-endpoints)
  - [12. Code Link Endpoints](#12-code-link-endpoints)
  - [13. Space GitHub Repo Endpoints](#13-space-github-repo-endpoints)
  - [Error Response Pattern](#error-response-pattern)

---

## Overview

This document contains only the APIs currently used by the SPA API clients in the **`jira-agentic-ai-web`** repository (`src/api/`).

- Base URL: `/api`
- Auth: `Authorization: Bearer {access_token}` for protected endpoints
- Content-Type: `application/json` unless otherwise specified

---

## Conventions

- `issueKey` is business key (example: `PLT-42`)
- `issueId` is numeric database id
- Most create/update endpoints currently return `200 OK` in current backend implementation
- Delete endpoints return `204 No Content`

### Persistence (data layer) in endpoint sections

Under **`Persistence (data layer)`**, each route documents:

- **`Tables:`** — primary table(s) this HTTP call is *about* (the resource you list or mutate).
- **`Flow:`** — **`Controller.method` → `Service.method` → `Repository.method`** aligned with the current Spring Boot code. Side paths (JWT issuance, S3, GitHub HTTP) are not expanded in **`Flow:`**; read the service for those. Probes with no DB (e.g. OAuth2 status) use **`Tables:` `N/A`** and state **no `JpaRepository`** in **`Flow:`**.

---

## Purpose Definition Standard (Apply To Every API)

To make each endpoint purpose clear in real-world usage, evaluate it with four questions:

1. **When this API is called (trigger scenario):**  
   Which UI action, background workflow, or integration event invokes it.
2. **Why this API is called (business intent):**  
   The user/system goal it serves (create data, fetch data for rendering, enforce relation consistency, etc.).
3. **What this API does after being called (system effect):**  
   What is read/validated/changed, and what response the caller can use next.
4. **Which persistence it touches:**  
   Use **`Tables:`** and **`Flow:`** under **`Persistence (data layer)`** on each detailed route (see [Persistence (data layer) in endpoint sections](#persistence-data-layer-in-endpoint-sections)).

Use this as the checklist when reviewing each endpoint below.

---

## API Invocation Scenarios (All Endpoints Checklist)

This section adds the "when/why/what" for every API family so readers can understand call intent, not only request/response shape.

### 1) Auth

- `POST /api/auth/login` - **When:** legacy login form submit;

**Why:** verify identity for profile session; **What:** validates credentials and returns user profile payload.
- `POST /api/auth/token` - **When:** token-based login submit;

**Why:** obtain bearer token for protected calls; **What:** authenticates user and returns JWT + expiry + user summary.
- `GET /api/auth/oauth2/status` - **When:** app bootstrap / OAuth callback checks;

**Why:** decide whether to show login or app; **What:** returns authenticated status (and user identity if signed in).

### 2) Users

- `GET /api/users` - **When:** assignee/reporter/member pickers open;

**Why:** populate selectable users; **What:** returns user list for UI options.
- `GET /api/users/{id}` - **When:** profile/owner display needs one user;

**Why:** resolve a specific user reference; **What:** returns one user DTO.
- `POST /api/users` - **When:** admin or setup flow creates account;

**Why:** register a new actor in the system; **What:** persists user and returns created user summary.
- `DELETE /api/users/{id}` - **When:** admin removes account;

**Why:** deprovision user access; **What:** deletes user record (204).

### 3) Spaces

- `GET /api/spaces` - **When:** sidebar/project switcher loads;

**Why:** user can switch space from the list; **What:** returns `SpaceDto[]` for the picker.
- `GET /api/spaces/{id}` - **When:** entering one space context;

**Why:** fetch space metadata; **What:** returns the selected space.
- `POST /api/spaces?ownerId=...` - **When:** "Create Space" form submit;

**Why:** start a new project scope; **What:** creates space and returns new space data.
- `PUT /api/spaces/{id}` - **When:** space settings edited;

**Why:** rename/rebrand/update key/color; **What:** updates metadata and returns latest state.
- `DELETE /api/spaces/{id}` - **When:** owner/admin removes space;

**Why:** retire project scope; **What:** soft/hard delete per backend policy (204).
- `GET /api/spaces/{id}/members` - **When:** permissions panel opens;

**Why:** view direct membership; **What:** returns member list.
- `POST /api/spaces/{id}/members` - **When:** add member action;

**Why:** grant user access; **What:** creates membership relation.
- `DELETE /api/spaces/{id}/members/{userId}` - **When:** remove member action;

**Why:** revoke user access; **What:** deletes membership relation.
- `GET /api/spaces/{id}/groups` - **When:** space-group mapping page loads;

**Why:** inspect group-based access; **What:** returns linked groups.
- `POST /api/spaces/{id}/groups` - **When:** link group to space;

**Why:** grant access to many users via group; **What:** creates space-group relation.
- `DELETE /api/spaces/{id}/groups/{groupId}` - **When:** unlink group;

**Why:** revoke inherited access path; **What:** removes space-group relation.

### 4) Groups

- `GET /api/groups` - **When:** group management page loads;

**Why:** browse available groups; **What:** returns all groups.
- `GET /api/groups/{id}` - **When:** group detail page opens;

**Why:** inspect one group; **What:** returns group metadata and member context.
- `POST /api/groups?ownerId=...` - **When:** create team/group action;

**Why:** define reusable permission set; **What:** creates group.
- `PUT /api/groups/{id}` - **When:** group info edited;

**Why:** maintain naming/description; **What:** updates and returns group.
- `DELETE /api/groups/{id}` - **When:** group is retired;

**Why:** remove obsolete access structure; **What:** deletes group.
- `GET /api/groups/{id}/members` - **When:** membership editor opens;

**Why:** audit current members; **What:** returns users in group.
- `POST /api/groups/{id}/members` - **When:** add user to group;

**Why:** grant permissions via group inheritance; **What:** creates membership.
- `DELETE /api/groups/{id}/members/{userId}` - **When:** remove user from group;

**Why:** revoke inherited permissions; **What:** removes membership.

### 5) Sprints

- `GET /api/spaces/{spaceId}/sprints` - **When:** board/backlog sprint selector loads;

**Why:** show planning cycles; **What:** returns sprint list in space.
- `GET /api/spaces/{spaceId}/sprints/{id}` - **When:** sprint detail opens;

**Why:** view one sprint's schedule/state; **What:** returns one sprint.
- `POST /api/spaces/{spaceId}/sprints` - **When:** sprint planning creates cycle;

**Why:** define execution window; **What:** creates sprint.
- `PUT /api/spaces/{spaceId}/sprints/{id}` - **When:** sprint timeline/status changes;

**Why:** keep plan current; **What:** updates sprint (and related behavior as implemented).
- `DELETE /api/spaces/{spaceId}/sprints/{id}` - **When:** invalid/obsolete sprint removed;

**Why:** clean planning artifacts; **What:** deletes sprint.

### 6) Issues

- `GET /api/spaces/{spaceId}/issues` - **When:** board/backlog/ticket list loads;

**Why:** render work items; **What:** returns issues in space.
- `GET /api/spaces/{spaceId}/issues/{issueKey}` - **When:** opening ticket detail by key;

**Why:** view one issue complete context; **What:** returns issue DTO.
- `POST /api/spaces/{spaceId}/issues` - **When:** create ticket form submit;

**Why:** track new unit of work; **What:** creates issue and assigns generated key.
- `PUT /api/spaces/{spaceId}/issues/{issueKey}` - **When:** status/assignee/labels/etc. edited;

**Why:** progress and reprioritize work; **What:** updates fields and returns latest issue.
- `DELETE /api/spaces/{spaceId}/issues/{issueKey}` - **When:** ticket should be removed;

**Why:** eliminate invalid/obsolete work item; **What:** deletes issue (and related dependency cleanup per service logic).

### 7) Comments

- `GET /api/issues/{issueId}/comments` - **When:** ticket activity panel opens;

**Why:** show discussion thread; **What:** returns comment timeline.
- `POST /api/issues/{issueId}/comments` - **When:** user submits comment;

**Why:** capture collaboration context; **What:** creates and returns comment.
- `PUT /api/issues/{issueId}/comments/{commentId}` - **When:** comment edit action;

**Why:** correct/update comment content; **What:** updates and returns comment.
- `DELETE /api/issues/{issueId}/comments/{commentId}` - **When:** comment moderation/removal;

**Why:** remove obsolete/inappropriate text; **What:** deletes comment.

### 8) Attachments

- `GET /api/issues/{issueId}/attachments` - **When:** attachments panel opens;

**Why:** list evidence/docs linked to issue; **What:** returns attachment metadata list.
- `POST /api/issues/{issueId}/attachments` - **When:** file upload action;

**Why:** attach logs/screenshots/spec files to issue; **What:** stores file + metadata row and returns attachment DTO.
- `GET /api/issues/{issueId}/attachments/{attachmentId}/download` - **When:** user clicks download;

**Why:** retrieve original file content; **What:** streams binary payload.
- `DELETE /api/issues/{issueId}/attachments/{attachmentId}` - **When:** remove bad/old file;

**Why:** clean issue artifacts; **What:** deletes metadata and backing file per implementation.

### 9) Work Logs

- `GET /api/issues/{issueId}/worklogs` - **When:** effort/history panel loads;

**Why:** inspect time spent; **What:** returns work log entries.
- `POST /api/issues/{issueId}/worklogs` - **When:** log time action submit;

**Why:** track effort against issue; **What:** creates work log entry.
- `PUT /api/issues/{issueId}/worklogs/{workLogId}` - **When:** correcting logged effort;

**Why:** maintain accurate reporting; **What:** updates work log entry.
- `DELETE /api/issues/{issueId}/worklogs/{workLogId}` - **When:** invalid entry removal;

**Why:** clean incorrect effort data; **What:** deletes work log entry.

### 10) Issue History

- `GET /api/issues/{issueId}/history` - **When:** change timeline opens;

**Why:** audit who changed what and when; **What:** returns ordered history events.

### 11) Issue Links

- `GET /api/issues/{issueId}/links` - **When:** dependency panel loads;

**Why:** visualize blockers/relations; **What:** returns issue relation list.
- `POST /api/issues/{issueId}/links` - **When:** link issues action;

**Why:** encode dependency/relationship semantics; **What:** creates link and returns resulting relation DTO.
- `DELETE /api/issues/{issueId}/links/{linkId}` - **When:** unlink action;

**Why:** remove obsolete dependency; **What:** deletes relation link.

### 12) Code Links + GitHub Integration

- `GET /api/issues/{issueId}/code-links` - **When:** ticket development panel loads;

**Why:** show linked PR/commit/repo context; **What:** returns code links on issue.
- `POST /api/issues/{issueId}/code-links` - **When:** user pastes GitHub URL;

**Why:** connect work item with implementation artifact; **What:** creates code link and optional metadata enrichment.
- `DELETE /api/issues/{issueId}/code-links/{linkId}` - **When:** unlink wrong/old development reference;

**Why:** keep dev context accurate; **What:** removes code link.
- `GET /api/spaces/{spaceId}/code-links` - **When:** space-level development dashboard loads;

**Why:** review development activity across issues; **What:** returns space-wide code links.
- `POST /api/spaces/{spaceId}/code-links/refresh` - **When:** bulk refresh action;

**Why:** sync latest GitHub states/titles/activity; **What:** checks and updates all links in space.
- `POST /api/issues/{issueId}/code-links/refresh` - **When:** issue-level refresh action;

**Why:** sync links for one ticket; **What:** refreshes metadata for that issue's links.
- `POST /api/code-links/{linkId}/refresh` - **When:** targeted refresh action;

**Why:** re-sync one stale link; **What:** refreshes and returns updated link DTO.
- `GET /api/spaces/{spaceId}/github-repos` - **When:** repo integration settings open;

**Why:** show configured repositories; **What:** returns connected repos.
- `POST /api/spaces/{spaceId}/github-repos` - **When:** add repository integration;

**Why:** enable scan/auto-link from that repo; **What:** creates repo mapping.
- `POST /api/spaces/{spaceId}/github-repos/bulk` - **When:** bootstrap integrations for account/org;

**Why:** reduce manual repo setup; **What:** discovers/repos and returns added/skipped stats.
- `DELETE /api/spaces/{spaceId}/github-repos/{repoId}` - **When:** disconnect repository;

**Why:** stop using repo for scans/linking; **What:** removes mapping (and dependent behavior per implementation).
- `POST /api/spaces/{spaceId}/github-repos/scan` - **When:** manual or scheduled scan;

**Why:** discover issue references and keep links up to date; **What:** scans repo activity and returns scan stats.

---

## Response Body Templates (Shared)

Use this section whenever an endpoint line currently shows only `SpaceDto` / `IssueDto[]` etc.  
The endpoint-specific section still defines status code and semantics; these are concrete response body shapes.

### Success Envelope For Mutations (recommended for readability)

```json
{
  "success": true,
  "message": "Operation completed",
  "data": {}
}
```

### Delete Response Template (if switching from 204 to body response)

> Current backend behavior for delete APIs is `204 No Content`.  
> If you want body-based consistency for docs/UI debugging, use `200 OK` with:

```json
{
  "success": true,
  "message": "Resource deleted",
  "data": {
    "resourceType": "issue",
    "resourceId": "PLT-42"
  }
}
```

### `SpaceDto`

```json
{
  "id": 10,
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED",
  "ownerId": 1
}
```

### `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": [
    {
      "id": 2,
      "username": "alice",
      "name": "Alice",
      "email": "alice@example.com",
      "avatarColor": "#8E44AD"
    }
  ]
}
```

### `SprintDto`

```json
{
  "id": 101,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail updates",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

### `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "description": "<p>Repro in board view...</p>",
  "issueType": "BUG",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101,
  "parentId": null,
  "storyPoints": 5,
  "labels": ["board", "urgent"],
  "issueOrder": 120
}
```

### `CommentDto`

```json
{
  "id": 2001,
  "issueId": 1001,
  "authorId": 2,
  "authorName": "Alice",
  "content": "<p>Root cause identified.</p>",
  "createdAt": "2026-04-29T20:00:00Z",
  "updatedAt": "2026-04-29T20:05:00Z"
}
```

### `IssueAttachmentDto`

```json
{
  "id": 5001,
  "issueId": 1001,
  "originalFilename": "stacktrace.png",
  "contentType": "image/png",
  "sizeBytes": 245123,
  "createdAt": "2026-04-29T21:00:00Z",
  "uploaderId": 2
}
```

### `WorkLogDto`

```json
{
  "id": 7001,
  "issueId": 1001,
  "authorId": 2,
  "spentMinutes": 120,
  "note": "Added tests and refactor",
  "logDate": "2026-04-29",
  "createdAt": "2026-04-29T22:00:00Z"
}
```

### `IssueLinkDto`

```json
{
  "id": 801,
  "relation": "blocks",
  "linkedIssueId": 1002,
  "linkedIssueKey": "PLT-43",
  "linkedIssueTitle": "Refactor drag handlers",
  "createdAt": "2026-04-29T20:30:00Z"
}
```

### `IssueCodeLinkDto`

```json
{
  "id": 9001,
  "issueId": 1001,
  "url": "https://github.com/org/repo/pull/123",
  "provider": "github",
  "kind": "pull_request",
  "owner": "org",
  "repo": "repo",
  "refId": "123",
  "title": "Fix drag drop order",
  "state": "open",
  "authorLogin": "alice",
  "lastActivityAt": "2026-04-29T23:00:00Z"
}
```

### `SpaceGithubRepoDto`

```json
{
  "id": 1,
  "spaceId": 10,
  "owner": "my-org",
  "repo": "my-repo",
  "createdAt": "2026-04-28T10:00:00Z",
  "lastScannedAt": null
}
```

---

## 1. Auth Endpoints

### 1.1 Issue JWT Token

**Purpose:** Authenticate with username/password and issue JWT used by the frontend.

**When called:** user submits login form in token mode.

**Why:** app needs bearer token for protected APIs.

**After call:** backend validates credentials and returns token + user profile used by frontend session state.

**Persistence (data layer)**

**Tables:** `users`
**Flow:** `AuthController.issueToken` → `UserService.authenticate` → `UserRepository.findByUsername`


**Endpoint:** `/api/auth/token`  
**Method:** `POST`

**Request Body:**

```json
{
  "username": "alice",
  "password": "secret123"
}
```

**Response:** (200 OK)

```json
{
  "accessToken": "eyJhbGciOi...",
  "tokenType": "Bearer",
  "expiresInMinutes": 120,
  "user": {
    "id": 1,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#5B8DEF"
  }
}
```

**Error Response:** (400 Bad Request)  
**When:** `username` or `password` is missing in request body.

```json
{
  "message": "Username and password are required"
}
```

**Error Response:** (401 Unauthorized)  
**When:** Username/password is provided but authentication fails.

```json
{
  "message": "Invalid username or password"
}
```

**Frontend Handling Recommendation:**

- `400`: show "Please enter both username and password."
- `401`: show "Invalid username or password."

---

## 2. User Endpoints

### 2.1 Get All Users

**Purpose:** Retrieve all users for pickers and assignment.

**When called:** issue forms, member dialogs, and filter panels load.

**Why:** UI needs selectable user options.

**After call:** backend returns user list for assignee/reporter/member selection.

**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.getAll` → `UserService.findAll` → `UserRepository.findAll`


**Endpoint:** `/api/users`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 1,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#5B8DEF"
  }
]
```

---

### 2.2 Get User By ID

**Purpose:** Retrieve a single user by id.

**When called:** UI needs to resolve one specific user reference.

**Why:** display exact profile data for that id.

**After call:** backend returns one user DTO for rendering.

**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.getById` → `UserService.findById` → `UserRepository.findById`


**Endpoint:** `/api/users/{id}`  
**Method:** `GET`

**Path Parameters:**

- `id` (number)

**Response:** (200 OK)

```json
{
  "id": 1,
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "avatarColor": "#5B8DEF"
}
```

---

### 2.3 Create User

**Purpose:** Create a new user.

**When called:** admin/setup flow submits new account form.

**Why:** onboard a new system user.

**After call:** backend persists the user and returns created profile payload.

**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.create` → `UserService.create` → `UserRepository.save`


**Endpoint:** `/api/users`  
**Method:** `POST`

**Request Body:**

```json
{
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "password": "securepassword123",
  "avatarColor": "#8E44AD"
}
```

**Response:** (200 OK)

```json
{
  "id": 42,
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "avatarColor": "#8E44AD"
}
```

---

### 2.4 Delete User

**Purpose:** Delete a user.

**When called:** admin triggers account removal.

**Why:** revoke and remove an obsolete account.

**After call:** backend deletes user record and returns no-content success.

**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.delete` → `UserService.delete` → `UserRepository.deleteById`


**Endpoint:** `/api/users/{id}`  
**Method:** `DELETE`

**Path Parameters:**

- `id` (number)

**Response:** (204 No Content)

---

## 3. Space Endpoints

### 3.1 Get Spaces

**Purpose:** Retrieve spaces for the sidebar/picker as `SpaceDto[]`.

**When called:** sidebar/project selector loads.

**Why:** user can switch which space they work in.

**After call:** backend returns `SpaceDto[]`.

**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.getAll` → `SpaceService.listSpaces` → `SpaceRepository.findAllByDeletedAtIsNullOrderByIdAsc`


**Endpoint:** `/api/spaces`  
**Method:** `GET`

**Query Parameters:**

- `userId` (optional, number)

**Response:** (200 OK)

```json
[
  {
    "id": 10,
    "name": "Platform",
    "key": "PLT",
    "color": "#2F80ED",
    "ownerId": 1
  }
]
```

---

### 3.2 Get Space By ID

**Purpose:** Retrieve one space.

**When called:** entering space detail/settings context.

**Why:** UI needs canonical space metadata.

**After call:** backend returns one `SpaceDto`.

**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.getById` → `SpaceService.findById` → `SpaceRepository.findByIdAndDeletedAtIsNull`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `GET`

**Response:** (200 OK)

```json
{
  "id": 10,
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED",
  "ownerId": 1
}
```

---

### 3.3 Create Space

**Purpose:** Create a new space.

**When called:** user submits "Create Space" flow.

**Why:** initialize a new project/work area.

**After call:** backend creates space (with owner relation) and returns created space object.

**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.create` → `SpaceService.create` → `SpaceRepository.save`


**Endpoint:** `/api/spaces?ownerId={ownerId}`  
**Method:** `POST`

**Request Body:**

```json
{
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED"
}
```

**Response:** (200 OK)

```json
{
  "id": 10,
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED",
  "ownerId": 1
}
```

---

### 3.4 Update Space

**Purpose:** Update an existing space.

**When called:** space settings are edited.

**Why:** keep name/key/color metadata current.

**After call:** backend updates and returns the latest `SpaceDto`.

**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.update` → `SpaceService.update` → `SpaceRepository.save`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `PUT`

**Request Body:**

```json
{
  "name": "Platform Core",
  "key": "PLT",
  "color": "#1F6FD6"
}
```

**Response:** (200 OK) `SpaceDto`

```json
{
  "id": 10,
  "name": "Platform Core",
  "key": "PLT",
  "color": "#1F6FD6",
  "ownerId": 1
}
```

---

### 3.5 Delete Space

**Purpose:** Delete a space.

**When called:** owner/admin confirms space removal.

**Why:** retire an unused project scope.

**After call:** backend executes delete policy and returns no-content success.

**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.delete` → `SpaceService.delete` → `SpaceRepository.save`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

### 3.6 Get Space Members

**Purpose:** Retrieve direct members of a space.

**When called:** membership/permission panel opens.

**Why:** show who has direct access.

**After call:** backend returns `UserDto[]` for that space.

**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.getMembers` → `SpaceService.getMembers` → `SpaceMemberRepository.findBySpaceId`


**Endpoint:** `/api/spaces/{id}/members`  
**Method:** `GET`

**Response:** (200 OK) `UserDto[]`

```json
[
  {
    "id": 1,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#5B8DEF"
  },
  {
    "id": 2,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#8E44AD"
  }
]
```

---

### 3.7 Add Space Member

**Purpose:** Add user to space.

**When called:** admin adds a member from space settings.

**Why:** grant direct space access.

**After call:** backend creates membership relation and returns success.

**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.addMember` → `SpaceService.addMember` → `SpaceMemberRepository.save`


**Endpoint:** `/api/spaces/{id}/members`  
**Method:** `POST`

**Request Body:**

```json
{
  "userId": 2
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Member added to space",
  "data": {
    "spaceId": 10,
    "userId": 2
  }
}
```

---

### 3.8 Remove Space Member

**Purpose:** Remove user from space.

**When called:** admin removes a direct member.

**Why:** revoke direct access to space.

**After call:** backend deletes membership relation and returns no-content success.

**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.removeMember` → `SpaceService.removeMember` → `SpaceMemberRepository.deleteBySpaceIdAndUserId`


**Endpoint:** `/api/spaces/{id}/members/{userId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

### 3.9 Get Space Groups

**Purpose:** Retrieve groups linked to a space.

**When called:** space-group mapping view loads.

**Why:** inspect inherited group access.

**After call:** backend returns linked groups for that space.

**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.getSpaceGroups` → `SpaceService.getSpaceGroups` → `SpaceGroupRepository.findBySpaceId`


**Endpoint:** `/api/spaces/{id}/groups`  
**Method:** `GET`

**Response:** (200 OK) `GroupDto[]`

```json
[
  {
    "id": 5,
    "name": "Backend Team",
    "description": "Core backend maintainers",
    "ownerId": 1,
    "members": []
  }
]
```

---

### 3.10 Add Group To Space

**Purpose:** Link group to a space.

**When called:** admin links an existing group to space.

**Why:** grant access to many users via group membership.

**After call:** backend creates space-group relation and returns success.

**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.addGroup` → `SpaceService.addGroup` → `SpaceGroupRepository.save`


**Endpoint:** `/api/spaces/{id}/groups`  
**Method:** `POST`

**Request Body:**

```json
{
  "groupId": 5
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Group linked to space",
  "data": {
    "spaceId": 10,
    "groupId": 5
  }
}
```

---

### 3.11 Remove Group From Space

**Purpose:** Unlink group from a space.

**When called:** admin removes group-based access.

**Why:** stop inherited permissions from that group.

**After call:** backend deletes relation and returns no-content success.

**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.removeGroup` → `SpaceService.removeGroup` → `SpaceGroupRepository.deleteBySpaceIdAndGroupId`


**Endpoint:** `/api/spaces/{id}/groups/{groupId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 4. Group Endpoints

### 4.1 Get All Groups

**Purpose:** Retrieve all groups.

**When called:** group management pages and selectors load.

**Why:** list reusable permission groups.

**After call:** backend returns `GroupDto[]`.

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.getAll` → `GroupService.findAll` → `UserGroupRepository.findAllByOrderByIdAsc`


**Endpoint:** `/api/groups`  
**Method:** `GET`

**Response:** (200 OK) `GroupDto[]`

```json
[
  {
    "id": 5,
    "name": "Backend Team",
    "description": "Core backend maintainers",
    "ownerId": 1,
    "members": []
  }
]
```

---

### 4.2 Get Group By ID

**Purpose:** Retrieve one group.

**When called:** opening group detail page.

**Why:** inspect a specific group's metadata and members.

**After call:** backend returns one `GroupDto`.

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.getById` → `GroupService.findById` → `UserGroupRepository.findById`


**Endpoint:** `/api/groups/{id}`  
**Method:** `GET`

**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": [
    {
      "id": 2,
      "username": "alice",
      "name": "Alice",
      "email": "alice@example.com",
      "avatarColor": "#8E44AD"
    }
  ]
}
```

---

### 4.3 Create Group

**Purpose:** Create a group.

**When called:** admin submits create-group form.

**Why:** define reusable team/access container.

**After call:** backend creates group and returns created `GroupDto`.

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.create` → `GroupService.create` → `UserGroupRepository.save`


**Endpoint:** `/api/groups?ownerId={ownerId}`  
**Method:** `POST`

**Request Body:**

```json
{
  "name": "Backend Team",
  "description": "Core backend maintainers"
}
```

**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": []
}
```

---

### 4.4 Update Group

**Purpose:** Update a group.

**When called:** admin edits group info.

**Why:** keep group name/description accurate.

**After call:** backend updates and returns `GroupDto`.

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.update` → `GroupService.update` → `UserGroupRepository.save`


**Endpoint:** `/api/groups/{id}`  
**Method:** `PUT`

**Request Body:**

```json
{
  "name": "Backend Team",
  "description": "Updated description"
}
```

**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Platform Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": []
}
```

---

### 4.5 Delete Group

**Purpose:** Delete a group.

**When called:** obsolete group is removed.

**Why:** clean permission model.

**After call:** backend deletes group and returns no-content success.

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.delete` → `GroupService.delete` → `UserGroupRepository.deleteById`


**Endpoint:** `/api/groups/{id}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

### 4.6 Get Group Members

**Purpose:** Retrieve group members.

**When called:** group membership panel opens.

**Why:** audit current members.

**After call:** backend returns `UserDto[]` of group members.

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.getMembers` → `GroupService.getMembers` → `GroupMemberRepository.findByGroupId`


**Endpoint:** `/api/groups/{id}/members`  
**Method:** `GET`

**Response:** (200 OK) `UserDto[]`

```json
[
  {
    "id": 2,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#8E44AD"
  }
]
```

---

### 4.7 Add Group Member

**Purpose:** Add user to group.

**When called:** admin adds member in group settings.

**Why:** grant permissions via group inheritance.

**After call:** backend creates group-member relation and returns success.

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.addMember` → `GroupService.addMember` → `GroupMemberRepository.save`


**Endpoint:** `/api/groups/{id}/members`  
**Method:** `POST`

**Request Body:**

```json
{
  "userId": 2
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Member added to group",
  "data": {
    "groupId": 5,
    "userId": 2
  }
}
```

---

### 4.8 Remove Group Member

**Purpose:** Remove user from group.

**When called:** admin removes member from group.

**Why:** revoke inherited access from that group.

**After call:** backend deletes relation and returns no-content success.

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.removeMember` → `GroupService.removeMember` → `GroupMemberRepository.deleteByGroupIdAndUserId`


**Endpoint:** `/api/groups/{id}/members/{userId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 5. Sprint Endpoints

### 5.1 Get Sprints By Space

**Purpose:** Retrieve all sprints in a space.

**When called:** board/backlog sprint selector loads.

**Why:** display planning cycles for the space.

**After call:** backend returns sprint list ordered for UI use.

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.getBySpace` → `SprintService.findBySpace` → `SprintRepository.findBySpaceIdOrderByStartDateAsc`


**Endpoint:** `/api/spaces/{spaceId}/sprints`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 101,
    "spaceId": 10,
    "name": "Sprint 24",
    "goal": "Stabilize board workflow",
    "startDate": "2026-04-01",
    "endDate": "2026-04-14",
    "status": "ACTIVE"
  }
]
```

---

### 5.2 Get Sprint By ID

**Purpose:** Retrieve sprint by id.

**When called:** sprint detail/edit screen opens.

**Why:** load one sprint's schedule/status.

**After call:** backend returns one `SprintDto`.

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.getById` → `SprintService.findById` → `SprintRepository.findById`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `GET`

**Response:** (200 OK) `SprintDto`

```json
{
  "id": 101,
  "spaceId": 10,
  "name": "Sprint 24",
  "goal": "Stabilize board workflow",
  "startDate": "2026-04-01",
  "endDate": "2026-04-14",
  "status": "ACTIVE"
}
```

---

### 5.3 Create Sprint

**Purpose:** Create a sprint.

**When called:** planning flow creates a new iteration.

**Why:** define execution window and goal.

**After call:** backend creates sprint and returns `SprintDto`.

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.create` → `SprintService.create` → `SprintRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/sprints`  
**Method:** `POST`

**Request Body:**

```json
{
  "name": "Sprint 25",
  "goal": "Finish ticket-detail updates",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

**Response:** (200 OK) `SprintDto`

```json
{
  "id": 102,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail updates",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

---

### 5.4 Update Sprint

**Purpose:** Update sprint.

**When called:** sprint timeline/status/goal changes.

**Why:** keep planning state up to date.

**After call:** backend updates sprint and returns updated `SprintDto`.

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.update` → `SprintService.update` → `SprintRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `PUT`

**Request Body:** same shape as create

**Response:** (200 OK) `SprintDto`

```json
{
  "id": 102,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail updates",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "ACTIVE"
}
```

---

### 5.5 Delete Sprint

**Purpose:** Delete sprint.

**When called:** invalid or obsolete sprint is removed.

**Why:** clean planning artifacts.

**After call:** backend deletes sprint and returns no-content success.

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.delete` → `SprintService.delete` → `SprintRepository.deleteById`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 6. Issue Endpoints

### 6.1 Get Issues By Space

**Purpose:** Retrieve all issues in a space.

**When called:** board/backlog/ticket lists initialize.

**Why:** render work items for the space.

**After call:** backend returns issue collection for UI state.

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.getBySpace` → `IssueService.findBySpace` → `IssueRepository.findBySpaceIdOrderByIssueOrderAsc`


**Endpoint:** `/api/spaces/{spaceId}/issues`  
**Method:** `GET`

**Response:** (200 OK) `IssueDto[]`

```json
[
  {
    "id": 1001,
    "issueKey": "PLT-42",
    "spaceId": 10,
    "title": "Fix drag order mismatch",
    "status": "IN_PROGRESS",
    "priority": "HIGH",
    "assigneeId": 2,
    "reporterId": 1
  }
]
```

---

### 6.2 Get Issue By Key

**Purpose:** Retrieve issue by business key.

**When called:** user opens ticket detail by issue key.

**Why:** fetch canonical data for one issue.

**After call:** backend returns one `IssueDto`.

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.getByKey` → `IssueService.findByKey` → `IssueRepository.findByIssueKey`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `GET`

**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "description": "<p>Repro in board view...</p>",
  "issueType": "BUG",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101,
  "storyPoints": 5,
  "labels": ["board", "urgent"]
}
```

---

### 6.3 Create Issue

**Purpose:** Create issue in space.

**When called:** create-ticket form is submitted.

**Why:** track new work item.

**After call:** backend creates issue (with generated key/history) and returns created `IssueDto`.

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.create` → `IssueService.create` → `IssueRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/issues`  
**Method:** `POST`

**Request Body:**

```json
{
  "title": "Fix drag order mismatch",
  "description": "<p>Repro in board view...</p>",
  "issueType": "BUG",
  "status": "TODO",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101,
  "parentId": null,
  "storyPoints": 3,
  "startDate": "2026-04-29",
  "dueDate": "2026-05-02",
  "labels": ["board", "frontend"]
}
```

**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "status": "TODO",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101
}
```

---

### 6.4 Update Issue

**Purpose:** Update issue fields.

**When called:** status, assignee, labels, sprint, or ordering is edited.

**Why:** reflect current execution state.

**After call:** backend applies changes and returns updated `IssueDto`.

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.update` → `IssueService.update` → `IssueRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `PUT`

**Request Body:**

```json
{
  "status": "IN_PROGRESS",
  "assigneeId": 2,
  "clearAssignee": false,
  "clearReporter": false,
  "sprintId": 101,
  "clearSprint": false,
  "parentId": null,
  "clearParent": true,
  "storyPoints": 5,
  "issueOrder": 120,
  "labels": ["board", "urgent"]
}
```

**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101,
  "storyPoints": 5,
  "labels": ["board", "urgent"],
  "issueOrder": 120
}
```

---

### 6.5 Delete Issue

**Purpose:** Delete issue.

**When called:** user/admin confirms issue removal.

**Why:** remove invalid/obsolete work item.

**After call:** backend executes delete logic (including dependent cleanup) and returns no-content success.

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.delete` → `IssueService.delete` → `IssueRepository.delete`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 7. Comment Endpoints

### 7.1 Get Comments

**Purpose:** Retrieve comments for issue.

**When called:** issue activity/discussion tab opens.

**Why:** show collaboration thread.

**After call:** backend returns ordered `CommentDto[]`.

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.getByIssue` → `CommentService.findByIssue` → `CommentRepository.findByIssueIdOrderByCreatedAtAsc`


**Endpoint:** `/api/issues/{issueId}/comments`  
**Method:** `GET`

**Response:** (200 OK) `CommentDto[]`

```json
[
  {
    "id": 2001,
    "issueId": 1001,
    "authorId": 2,
    "authorName": "Alice",
    "content": "<p>I can reproduce this issue.</p>",
    "createdAt": "2026-04-29T20:00:00Z",
    "updatedAt": "2026-04-29T20:00:00Z"
  }
]
```

---

### 7.2 Create Comment

**Purpose:** Add a comment to issue.

**When called:** user submits new comment.

**Why:** persist discussion context.

**After call:** backend saves comment, records history event, and returns created `CommentDto`.

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.create` → `CommentService.create` → `CommentRepository.save`


**Endpoint:** `/api/issues/{issueId}/comments`  
**Method:** `POST`

**Request Body:**

```json
{
  "authorId": 2,
  "content": "<p>I can reproduce this issue.</p>"
}
```

**Response:** (200 OK) `CommentDto`

```json
{
  "id": 2001,
  "issueId": 1001,
  "authorId": 2,
  "authorName": "Alice",
  "content": "<p>I can reproduce this issue.</p>",
  "createdAt": "2026-04-29T20:00:00Z",
  "updatedAt": "2026-04-29T20:00:00Z"
}
```

---

### 7.3 Update Comment

**Purpose:** Update comment content.

**When called:** user edits an existing comment.

**Why:** correct or refine discussion text.

**After call:** backend updates comment and returns updated `CommentDto`.

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.update` → `CommentService.update` → `CommentRepository.save`


**Endpoint:** `/api/issues/{issueId}/comments/{commentId}`  
**Method:** `PUT`

**Request Body:**

```json
{
  "content": "<p>Root cause identified.</p>"
}
```

**Response:** (200 OK) `CommentDto`

```json
{
  "id": 2001,
  "issueId": 1001,
  "authorId": 2,
  "authorName": "Alice",
  "content": "<p>Root cause identified.</p>",
  "createdAt": "2026-04-29T20:00:00Z",
  "updatedAt": "2026-04-29T20:05:00Z"
}
```

---

### 7.4 Delete Comment

**Purpose:** Delete comment.

**When called:** user/moderator removes a comment.

**Why:** clean irrelevant or wrong discussion entry.

**After call:** backend deletes comment and returns no-content success.

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.delete` → `CommentService.delete` → `CommentRepository.deleteById`


**Endpoint:** `/api/issues/{issueId}/comments/{commentId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 8. Attachment Endpoints

### 8.1 Get Attachments

**Purpose:** Retrieve issue attachments.

**When called:** attachment panel opens.

**Why:** list all files linked to the issue.

**After call:** backend returns `IssueAttachmentDto[]`.

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.getByIssue` → `IssueAttachmentService.findByIssue` → `IssueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/attachments`  
**Method:** `GET`

**Response:** (200 OK) `IssueAttachmentDto[]`

```json
[
  {
    "id": 5001,
    "issueId": 1001,
    "originalFilename": "stacktrace.png",
    "contentType": "image/png",
    "sizeBytes": 245123,
    "createdAt": "2026-04-29T21:00:00Z",
    "uploaderId": 2
  }
]
```

---

### 8.2 Upload Attachment

**Purpose:** Upload attachment to issue.

**When called:** user uploads file from issue detail.

**Why:** attach evidence/docs/assets to the ticket.

**After call:** backend stores file + metadata and returns created `IssueAttachmentDto`.

**Side effects (application — optional):** When **`APP_KAFKA_ATTACHMENT_INGESTION_ENABLED=true`**, the API service may publish a **tracking JSON** message to Kafka (**`jira.attachment.ingestion`** by default, configurable via **`APP_KAFKA_ATTACHMENT_INGESTION_TOPIC`**) **after the DB transaction commits** for this upload. There is **no** equivalent Kafka event for attachment **delete** in the current implementation.

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.upload` → `IssueAttachmentService.upload` → `IssueAttachmentRepository.save`


**Endpoint:** `/api/issues/{issueId}/attachments`  
**Method:** `POST`  
**Content-Type:** `multipart/form-data`

**Form Fields:**

- `file` (required)
- `embedded` (optional, `true|false`, default `false`)

**Response:** (200 OK) `IssueAttachmentDto`

```json
{
  "id": 5001,
  "issueId": 1001,
  "originalFilename": "stacktrace.png",
  "contentType": "image/png",
  "sizeBytes": 245123,
  "createdAt": "2026-04-29T21:00:00Z",
  "uploaderId": 2
}
```

---

### 8.3 Download Attachment

**Purpose:** Download attachment binary.

**When called:** user clicks download on an attachment.

**Why:** retrieve original file content.

**After call:** backend streams binary payload with attachment headers.

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.download` → `IssueAttachmentService.download` → `IssueAttachmentRepository.findById`


**Endpoint:** `/api/issues/{issueId}/attachments/{attachmentId}/download`  
**Method:** `GET`

**Response:** (200 OK, binary stream)

---

### 8.4 Delete Attachment

**Purpose:** Delete attachment.

**When called:** user removes outdated/incorrect file.

**Why:** keep issue artifacts clean.

**After call:** backend deletes metadata/file and returns no-content success.

**Important:** Deleting an attachment removes the **`issue_attachments`** row and the stored **binary** only. It **does not** automatically remove references embedded in **`issues.description`** or **`comments.content`** (e.g. HTML `data-attachment-id` / legacy `[attachment: …]` tokens). Clients may leave stale references until the user edits those fields.

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.delete` → `IssueAttachmentService.delete` → `IssueAttachmentRepository.delete`


**Endpoint:** `/api/issues/{issueId}/attachments/{attachmentId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 9. Work Log Endpoints

### 9.1 Get Work Logs

**Purpose:** Retrieve work logs for issue.

**When called:** effort/time tab opens.

**Why:** inspect logged effort entries.

**After call:** backend returns `WorkLogDto[]`.

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.getByIssue` → `WorkLogService.findByIssue` → `WorkLogRepository.findByIssueIdOrderByLogDateDescCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/worklogs`  
**Method:** `GET`

**Response:** (200 OK) `WorkLogDto[]`

```json
[
  {
    "id": 7001,
    "issueId": 1001,
    "authorId": 2,
    "spentMinutes": 90,
    "note": "Investigated sorting logic",
    "logDate": "2026-04-29",
    "createdAt": "2026-04-29T22:00:00Z"
  }
]
```

---

### 9.2 Create Work Log

**Purpose:** Create work log entry.

**When called:** user logs spent time.

**Why:** track effort against issue progress.

**After call:** backend creates work log and returns `WorkLogDto`.

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.create` → `WorkLogService.create` → `WorkLogRepository.save`


**Endpoint:** `/api/issues/{issueId}/worklogs`  
**Method:** `POST`

**Request Body:**

```json
{
  "authorId": 2,
  "spentMinutes": 90,
  "note": "Investigated sorting logic",
  "logDate": "2026-04-29"
}
```

**Response:** (200 OK) `WorkLogDto`

```json
{
  "id": 7001,
  "issueId": 1001,
  "authorId": 2,
  "spentMinutes": 90,
  "note": "Investigated sorting logic",
  "logDate": "2026-04-29",
  "createdAt": "2026-04-29T22:00:00Z"
}
```

---

### 9.3 Update Work Log

**Purpose:** Update work log entry.

**When called:** user corrects previously logged time/note/date.

**Why:** maintain accurate reporting.

**After call:** backend updates row and returns updated `WorkLogDto`.

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.update` → `WorkLogService.update` → `WorkLogRepository.save`


**Endpoint:** `/api/issues/{issueId}/worklogs/{workLogId}`  
**Method:** `PUT`

**Request Body:**

```json
{
  "spentMinutes": 120,
  "note": "Added tests and refactor",
  "logDate": "2026-04-29"
}
```

**Response:** (200 OK) `WorkLogDto`

```json
{
  "id": 7001,
  "issueId": 1001,
  "authorId": 2,
  "spentMinutes": 120,
  "note": "Added tests and refactor",
  "logDate": "2026-04-29",
  "createdAt": "2026-04-29T22:00:00Z"
}
```

---

### 9.4 Delete Work Log

**Purpose:** Delete work log entry.

**When called:** invalid work log is removed.

**Why:** correct time accounting data.

**After call:** backend deletes entry and returns no-content success.

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.delete` → `WorkLogService.delete` → `WorkLogRepository.deleteById`


**Endpoint:** `/api/issues/{issueId}/worklogs/{workLogId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 10. Issue History Endpoints

### 10.1 Get Issue History

**Purpose:** Retrieve issue history timeline.

**When called:** activity/history panel opens.

**Why:** audit what changed, by whom, and when.

**After call:** backend returns history events for rendering timeline.

**Persistence (data layer)**

**Tables:** `issue_history`
**Flow:** `IssueHistoryController.getByIssue` → `IssueHistoryService.findByIssue` → `IssueHistoryRepository.findByIssueIdOrderByCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/history`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 3001,
    "issueId": 1001,
    "actorId": 2,
    "actorName": "Alice",
    "eventType": "status_changed",
    "fieldName": "status",
    "fromValue": "TODO",
    "toValue": "IN_PROGRESS",
    "description": null,
    "createdAt": "2026-04-29T20:00:00Z"
  }
]
```

---

## 11. Issue Link Endpoints

### 11.1 Get Issue Links

**Purpose:** Retrieve links for issue.

**When called:** dependency/link section loads.

**Why:** visualize blockers and related issues.

**After call:** backend returns `IssueLinkDto[]`.

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.getByIssue` → `IssueLinkService.findByIssue` → `IssueLinkRepository.findBySourceIssueIdOrTargetIssueId`


**Endpoint:** `/api/issues/{issueId}/links`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 801,
    "relation": "blocks",
    "linkedIssueId": 1002,
    "linkedIssueKey": "PLT-43",
    "linkedIssueTitle": "Refactor drag handlers",
    "createdAt": "2026-04-29T20:30:00Z"
  }
]
```

---

### 11.2 Create Issue Link

**Purpose:** Create relation between two issues.

**When called:** user links one issue to another.

**Why:** encode dependency/relationship semantics.

**After call:** backend creates relation and returns created `IssueLinkDto`.

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.create` → `IssueLinkService.create` → `IssueLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/links`  
**Method:** `POST`

**Request Body:**

```json
{
  "relation": "blocks",
  "targetIssueKey": "PLT-43"
}
```

**Response:** (200 OK) `IssueLinkDto`

```json
{
  "id": 801,
  "relation": "blocks",
  "linkedIssueId": 1002,
  "linkedIssueKey": "PLT-43",
  "linkedIssueTitle": "Refactor drag handlers",
  "createdAt": "2026-04-29T20:30:00Z"
}
```

---

### 11.3 Delete Issue Link

**Purpose:** Delete issue relation link.

**When called:** relation is no longer valid.

**Why:** remove obsolete dependency mapping.

**After call:** backend deletes link and returns no-content success.

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.delete` → `IssueLinkService.delete` → `IssueLinkRepository.delete`


**Endpoint:** `/api/issues/{issueId}/links/{linkId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 12. Code Link Endpoints

### 12.1 Get Issue Code Links

**Purpose:** Retrieve code links attached to issue.

**When called:** development panel opens on issue detail.

**Why:** show PR/commit/repo context for that ticket.

**After call:** backend returns `IssueCodeLinkDto[]`.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.getByIssue` → `IssueCodeLinkService.findByIssue` → `IssueCodeLinkRepository.findByIssueIdOrderByActivityDesc`


**Endpoint:** `/api/issues/{issueId}/code-links`  
**Method:** `GET`

**Response:** (200 OK) `IssueCodeLinkDto[]`

```json
[
  {
    "id": 9001,
    "issueId": 1001,
    "url": "https://github.com/org/repo/pull/123",
    "provider": "github",
    "kind": "pull_request",
    "owner": "org",
    "repo": "repo",
    "refId": "123",
    "title": "Fix drag drop order",
    "state": "open",
    "authorLogin": "alice",
    "lastActivityAt": "2026-04-29T23:00:00Z"
  }
]
```

---

### 12.2 Create Issue Code Link

**Purpose:** Attach a GitHub code link (PR/commit/repo/etc.) to issue.

**When called:** user pastes a GitHub URL into issue development section.

**Why:** bind implementation artifact to issue.

**After call:** backend creates link (with metadata enrichment if available) and returns `IssueCodeLinkDto`.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.create` → `IssueCodeLinkService.create` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/code-links`  
**Method:** `POST`

**Request Body:**

```json
{
  "url": "https://github.com/org/repo/pull/123",
  "githubToken": "github_pat_optional_for_private_repo"
}
```

**Response:** (200 OK) `IssueCodeLinkDto`

```json
{
  "id": 9001,
  "issueId": 1001,
  "url": "https://github.com/org/repo/pull/123",
  "provider": "github",
  "kind": "pull_request",
  "owner": "org",
  "repo": "repo",
  "refId": "123",
  "title": "Fix drag drop order",
  "state": "open",
  "authorLogin": "alice",
  "lastActivityAt": "2026-04-29T23:00:00Z"
}
```

---

### 12.3 Delete Issue Code Link

**Purpose:** Remove code link from issue.

**When called:** incorrect or stale dev link is removed.

**Why:** keep issue-development mapping accurate.

**After call:** backend deletes code link and returns no-content success.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.delete` → `IssueCodeLinkService.delete` → `IssueCodeLinkRepository.delete`


**Endpoint:** `/api/issues/{issueId}/code-links/{linkId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

### 12.4 Get Space Code Links

**Purpose:** Retrieve code links across a space.

**When called:** space-wide code/development dashboard loads.

**Why:** inspect implementation activity across issues.

**After call:** backend returns `IssueCodeLinkDto[]` for the space.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.getBySpace` → `IssueCodeLinkService.findBySpace` → `IssueCodeLinkRepository.findByIssueSpaceIdOrderByActivityDesc`


**Endpoint:** `/api/spaces/{spaceId}/code-links`  
**Method:** `GET`

**Response:** (200 OK) `IssueCodeLinkDto[]`

```json
[
  {
    "id": 9001,
    "issueId": 1001,
    "url": "https://github.com/org/repo/pull/123",
    "provider": "github",
    "kind": "pull_request",
    "owner": "org",
    "repo": "repo",
    "refId": "123",
    "title": "Fix drag drop order",
    "state": "open",
    "authorLogin": "alice",
    "lastActivityAt": "2026-04-29T23:00:00Z"
  }
]
```

---

### 12.5 Refresh Space Code Links

**Purpose:** Refresh metadata for code links in a space.

**When called:** user triggers bulk refresh in space view.

**Why:** sync latest PR/commit status/title/activity from provider.

**After call:** backend re-fetches metadata and returns checked/updated counts.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshSpace` → `IssueCodeLinkService.refreshSpace` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/code-links/refresh`  
**Method:** `POST`

**Request Body:** (optional)

```json
{
  "githubToken": "github_pat_optional"
}
```

**Response:** (200 OK)

```json
{
  "checked": 12,
  "updated": 9
}
```

---

### 12.6 Refresh Issue Code Links

**Purpose:** Refresh metadata for one issue's code links.

**When called:** user refreshes code links from a single issue.

**Why:** sync issue-level development metadata.

**After call:** backend refreshes links on that issue and returns checked/updated counts.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshIssue` → `IssueCodeLinkService.refreshIssue` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/code-links/refresh`  
**Method:** `POST`

**Request Body:** optional token body

**Response:** (200 OK)

```json
{
  "checked": 4,
  "updated": 3
}
```

---

### 12.7 Refresh One Code Link

**Purpose:** Refresh metadata for one code link.

**When called:** user refreshes one specific link row.

**Why:** fix stale status/title on targeted link.

**After call:** backend re-fetches provider metadata and returns updated `IssueCodeLinkDto`.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshOne` → `IssueCodeLinkService.refreshOne` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/code-links/{linkId}/refresh`  
**Method:** `POST`

**Request Body:** optional token body

**Response:** (200 OK) `IssueCodeLinkDto`

```json
{
  "id": 9001,
  "issueId": 1001,
  "url": "https://github.com/org/repo/pull/123",
  "provider": "github",
  "kind": "pull_request",
  "owner": "org",
  "repo": "repo",
  "refId": "123",
  "title": "Fix drag drop order",
  "state": "closed",
  "authorLogin": "alice",
  "lastActivityAt": "2026-04-30T10:20:00Z"
}
```

---

## 13. Space GitHub Repo Endpoints

### 13.1 List Space GitHub Repositories

**Purpose:** Retrieve configured GitHub repos for a space.

**When called:** integration settings screen loads.

**Why:** show currently connected repositories.

**After call:** backend returns repo mappings for that space.

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.list` → `GithubScanService.listRepos` → `SpaceGithubRepoRepository.findBySpaceIdOrderByCreatedAtAsc`


**Endpoint:** `/api/spaces/{spaceId}/github-repos`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 1,
    "spaceId": 10,
    "owner": "my-org",
    "repo": "my-repo",
    "createdAt": "2026-04-28T10:00:00Z",
    "lastScannedAt": null
  }
]
```

---

### 13.2 Add Space GitHub Repository

**Purpose:** Add one GitHub repo to space.

**When called:** user connects a repo manually.

**Why:** enable scan/linking from that repository.

**After call:** backend creates repo mapping and returns `SpaceGithubRepoDto`.

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.add` → `GithubScanService.addRepo` → `SpaceGithubRepoRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos`  
**Method:** `POST`

**Request Body:**

```json
{
  "target": "my-org/my-repo"
}
```

**Response:** (200 OK) `SpaceGithubRepoDto`

```json
{
  "id": 1,
  "spaceId": 10,
  "owner": "my-org",
  "repo": "my-repo",
  "createdAt": "2026-04-28T10:00:00Z",
  "lastScannedAt": null
}
```

---

### 13.3 Bulk Import Space GitHub Repositories

**Purpose:** Bulk-import repos from user/org account.

**When called:** user wants one-shot onboarding of many repos.

**Why:** reduce manual repo connection steps.

**After call:** backend discovers repos, adds new ones, and returns discovered/added/skipped counts.

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.bulkImport` → `GithubScanService.bulkAddReposFromAccount` → `SpaceGithubRepoRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/bulk`  
**Method:** `POST`

**Request Body:**

```json
{
  "account": "my-org",
  "githubToken": "github_pat_optional"
}
```

**Response:** (200 OK)

```json
{
  "discovered": 25,
  "added": 18,
  "skipped": 7
}
```

---

### 13.4 Remove Space GitHub Repository

**Purpose:** Remove one configured repo from space.

**When called:** user disconnects a repository integration.

**Why:** stop scans/associations from that repo.

**After call:** backend removes mapping and returns no-content success.

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.remove` → `GithubScanService.removeRepo` → `SpaceGithubRepoRepository.delete`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/{repoId}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

### 13.5 Scan Space GitHub Repositories

**Purpose:** Scan configured repos and create/update issue code links.

**When called:** user triggers manual sync (or scheduled job calls equivalent).

**Why:** discover issue references and refresh code-link graph.

**After call:** backend scans repo activity and returns scan stats payload.

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `SpaceGithubRepoController.scan` → `GithubScanService.scanSpace` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/scan`  
**Method:** `POST`

**Request Body:** (optional)

```json
{
  "githubToken": "github_pat_optional"
}
```

**Response:** (200 OK)

```json
{
  "reposScanned": 4,
  "reposRemoved": 0,
  "prsInspected": 120,
  "commitsInspected": 650,
  "linksCreated": 14,
  "perRepo": [],
  "warnings": []
}
```

---

## Error Response Pattern

Most errors follow:

```json
{
  "message": "Error detail"
}
```

Common status codes:

- `400 Bad Request`
- `401 Unauthorized`
- `403 Forbidden`
- `404 Not Found`
- `409 Conflict`
- `500 Internal Server Error`

# Jira-like Core API Design (Final, Non-AI)

## Table of Contents

- [Jira-like Core API Design (Final, Non-AI)](#jira-like-core-api-design-final-non-ai)
  - [Overview](#overview)
  - [Conventions](#conventions)
  - [1) Auth APIs](#1-auth-apis)
  - [2) User APIs](#2-user-apis)
  - [3) Space APIs](#3-space-apis)
  - [4) Group APIs](#4-group-apis)
  - [5) Sprint APIs](#5-sprint-apis)
  - [6) Issue APIs](#6-issue-apis)
  - [7) Comment APIs](#7-comment-apis)
  - [8) Attachment APIs](#8-attachment-apis)
  - [9) Work Log APIs](#9-work-log-apis)
  - [10) Issue History APIs](#10-issue-history-apis)
  - [11) Issue Link APIs](#11-issue-link-apis)
  - [12) Code Link / GitHub Integration APIs](#12-code-link--github-integration-apis)
  - [Error Response Pattern](#error-response-pattern)

---

## Overview

This is the final API design document for the current traditional Jira-like full-stack backend (Spring Boot), excluding future dedicated AI services.

- Base URL: `/api`
- Auth: Bearer JWT from `/api/auth/token`
- Content type: `application/json` unless otherwise stated

---

## Conventions

- `issueKey` = business key like `PLT-42`
- `issueId` = numeric database ID (used by comments/attachments/worklogs/history/links)
- Most write endpoints currently return `200 OK` in implementation (not `201`)
- Delete endpoints return `204 No Content`

### Persistence (data layer) in endpoint sections

Same as the [Frontend-Used document → Persistence](#persistence-data-layer-in-endpoint-sections): **`Tables:`** + **`Flow:`** (`Controller` → `Service` → `Repository`) on each `###` route below.

---

## 1) Auth APIs

### 1.1 Login (Legacy Profile Login)

**Purpose:** Validate username/password and return user profile.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `AuthController` (login) → `UserService.authenticate` → `UserRepository.findByUsername`


**Endpoint:** `/api/auth/login`  
**Method:** `POST`

**Request Body:**

```json
{
  "username": "alice",
  "password": "secret123"
}
```

**Response:** (200 OK)

```json
{
  "id": 1,
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "avatarColor": "#5B8DEF"
}
```

**Errors:** `400`, `401`

---

### 1.2 Issue JWT Token

**Purpose:** Authenticate and issue bearer JWT for protected APIs.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `AuthController.issueToken` → `UserService.authenticate` → `UserRepository.findByUsername`


**Endpoint:** `/api/auth/token`  
**Method:** `POST`

**Request Body:**

```json
{
  "username": "alice",
  "password": "secret123"
}
```

**Response:** (200 OK)

```json
{
  "accessToken": "eyJhbGciOi...",
  "tokenType": "Bearer",
  "expiresInMinutes": 120,
  "user": {
    "id": 1,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#5B8DEF"
  }
}
```

**Errors:** `400`, `401`

---

### 1.3 OAuth2 Status

**Purpose:** Return current OAuth2 auth status.


**Persistence (data layer)**

**Tables:** `N/A`
**Flow:** `GET /api/auth/oauth2/status` — **no `JpaRepository`**


**Endpoint:** `/api/auth/oauth2/status`  
**Method:** `GET`

**Response:** (200 OK)

```json
{
  "authenticated": false
}
```

or

```json
{
  "authenticated": true,
  "name": "Alice",
  "email": "alice@example.com"
}
```

---

## 2) User APIs

### 2.1 Get All Users

**Purpose:** Retrieve all users.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.getAll` → `UserService.findAll` → `UserRepository.findAll`


**Endpoint:** `/api/users`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 1,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#5B8DEF"
  }
]
```

---

### 2.2 Get User By ID

**Purpose:** Retrieve one user by id.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.getById` → `UserService.findById` → `UserRepository.findById`


**Endpoint:** `/api/users/{id}`  
**Method:** `GET`

**Path Parameters:**

- `id` (Long)

**Response:** (200 OK) `UserDto`

```json
{
  "id": 1,
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "avatarColor": "#5B8DEF"
}
```

---

### 2.3 Create User

**Purpose:** Create a new user account.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.create` → `UserService.create` → `UserRepository.save`


**Endpoint:** `/api/users`  
**Method:** `POST`

**Request Body:** (`CreateUserRequest`)

```json
{
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "password": "securepassword123",
  "avatarColor": "#8E44AD"
}
```

**Response:** (200 OK)

```json
{
  "id": 42,
  "username": "alice",
  "name": "Alice",
  "email": "alice@example.com",
  "avatarColor": "#8E44AD"
}
```

---

### 2.4 Delete User

**Purpose:** Delete user.


**Persistence (data layer)**

**Tables:** `users`
**Flow:** `UserController.delete` → `UserService.delete` → `UserRepository.deleteById`


**Endpoint:** `/api/users/{id}`  
**Method:** `DELETE`

**Response:** (204 No Content)

---

## 3) Space APIs

### 3.1 Get Spaces

**Purpose:** Retrieve spaces as `SpaceDto[]` for the picker.


**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.getAll` → `SpaceService.listSpaces` → `SpaceRepository.findAllByDeletedAtIsNullOrderByIdAsc`


**Endpoint:** `/api/spaces`  
**Method:** `GET`

**Query Parameters:**

- `userId` (optional)

**Response:** (200 OK) `SpaceDto[]`

```json
[
  {
    "id": 10,
    "name": "Platform",
    "key": "PLT",
    "color": "#2F80ED",
    "ownerId": 1
  }
]
```

---

### 3.2 Get Space By ID

**Purpose:** Retrieve one space.


**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.getById` → `SpaceService.findById` → `SpaceRepository.findByIdAndDeletedAtIsNull`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `GET`

**Response:** (200 OK) `SpaceDto`

```json
{
  "id": 10,
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED",
  "ownerId": 1
}
```

---

### 3.3 Create Space

**Purpose:** Create a new space.


**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.create` → `SpaceService.create` → `SpaceRepository.save`


**Endpoint:** `/api/spaces?ownerId={ownerId}`  
**Method:** `POST`

**Request Body:** (`CreateSpaceRequest`)

```json
{
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED"
}
```

**Response:** (200 OK) `SpaceDto`

```json
{
  "id": 10,
  "name": "Platform",
  "key": "PLT",
  "color": "#2F80ED",
  "ownerId": 1
}
```

---

### 3.4 Update Space

**Purpose:** Update space metadata.


**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.update` → `SpaceService.update` → `SpaceRepository.save`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `PUT`

**Request Body:** (`CreateSpaceRequest`)

```json
{
  "name": "Platform Core",
  "key": "PLT",
  "color": "#1F6FD6"
}
```

**Response:** (200 OK) `SpaceDto`

```json
{
  "id": 10,
  "name": "Platform Core",
  "key": "PLT",
  "color": "#1F6FD6",
  "ownerId": 1
}
```

---

### 3.5 Delete Space

**Purpose:** Delete space.


**Persistence (data layer)**

**Tables:** `spaces`
**Flow:** `SpaceController.delete` → `SpaceService.delete` → `SpaceRepository.save`


**Endpoint:** `/api/spaces/{id}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

### 3.6 Get Space Members

**Purpose:** Get all direct members in a space.


**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.getMembers` → `SpaceService.getMembers` → `SpaceMemberRepository.findBySpaceId`


**Endpoint:** `/api/spaces/{id}/members`  
**Method:** `GET`  
**Response:** (200 OK) `UserDto[]`

```json
[
  {
    "id": 2,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#8E44AD"
  }
]
```

---

### 3.7 Add Space Member

**Purpose:** Add user to space.


**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.addMember` → `SpaceService.addMember` → `SpaceMemberRepository.save`


**Endpoint:** `/api/spaces/{id}/members`  
**Method:** `POST`

**Request Body:** (`AddMemberRequest`)

```json
{
  "userId": 2
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Member added to space",
  "data": {
    "spaceId": 10,
    "userId": 2
  }
}
```

---

### 3.8 Remove Space Member

**Purpose:** Remove user from space.


**Persistence (data layer)**

**Tables:** `space_members`
**Flow:** `SpaceController.removeMember` → `SpaceService.removeMember` → `SpaceMemberRepository.deleteBySpaceIdAndUserId`


**Endpoint:** `/api/spaces/{id}/members/{userId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

### 3.9 Get Space Groups

**Purpose:** Get groups linked to space.


**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.getSpaceGroups` → `SpaceService.getSpaceGroups` → `SpaceGroupRepository.findBySpaceId`


**Endpoint:** `/api/spaces/{id}/groups`  
**Method:** `GET`  
**Response:** (200 OK) `GroupDto[]`

```json
[
  {
    "id": 5,
    "name": "Backend Team",
    "description": "Core backend maintainers",
    "ownerId": 1,
    "members": []
  }
]
```

---

### 3.10 Add Group To Space

**Purpose:** Link group to space.


**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.addGroup` → `SpaceService.addGroup` → `SpaceGroupRepository.save`


**Endpoint:** `/api/spaces/{id}/groups`  
**Method:** `POST`

**Request Body:**

```json
{
  "groupId": 5
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Group linked to space",
  "data": {
    "spaceId": 10,
    "groupId": 5
  }
}
```

---

### 3.11 Remove Group From Space

**Purpose:** Unlink group from space.


**Persistence (data layer)**

**Tables:** `space_groups`
**Flow:** `SpaceController.removeGroup` → `SpaceService.removeGroup` → `SpaceGroupRepository.deleteBySpaceIdAndGroupId`


**Endpoint:** `/api/spaces/{id}/groups/{groupId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 4) Group APIs

### 4.1 Get All Groups

**Purpose:** List all groups.  

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.getAll` → `GroupService.findAll` → `UserGroupRepository.findAllByOrderByIdAsc`


**Endpoint:** `/api/groups`  
**Method:** `GET`  
**Response:** (200 OK) `GroupDto[]`

```json
[
  {
    "id": 5,
    "name": "Backend Team",
    "description": "Core backend maintainers",
    "ownerId": 1,
    "members": []
  }
]
```

### 4.2 Get Group By ID

**Purpose:** Get one group.  

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.getById` → `GroupService.findById` → `UserGroupRepository.findById`


**Endpoint:** `/api/groups/{id}`  
**Method:** `GET`  
**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": []
}
```

### 4.3 Create Group

**Purpose:** Create group.  

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.create` → `GroupService.create` → `UserGroupRepository.save`


**Endpoint:** `/api/groups?ownerId={ownerId}`  
**Method:** `POST`  
**Request Body:** (`CreateGroupRequest`)

```json
{
  "name": "Backend Team"
}
```

**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": []
}
```

### 4.4 Update Group

**Purpose:** Update group.  

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.update` → `GroupService.update` → `UserGroupRepository.save`


**Endpoint:** `/api/groups/{id}`  
**Method:** `PUT`  
**Request Body:** (`CreateGroupRequest`)  
**Response:** (200 OK) `GroupDto`

```json
{
  "id": 5,
  "name": "Backend Platform Team",
  "description": "Core backend maintainers",
  "ownerId": 1,
  "members": []
}
```

### 4.5 Delete Group

**Purpose:** Delete group.  

**Persistence (data layer)**

**Tables:** `user_groups`
**Flow:** `GroupController.delete` → `GroupService.delete` → `UserGroupRepository.deleteById`


**Endpoint:** `/api/groups/{id}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

### 4.6 Get Group Members

**Purpose:** List group members.  

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.getMembers` → `GroupService.getMembers` → `GroupMemberRepository.findByGroupId`


**Endpoint:** `/api/groups/{id}/members`  
**Method:** `GET`  
**Response:** (200 OK) `UserDto[]`

```json
[
  {
    "id": 2,
    "username": "alice",
    "name": "Alice",
    "email": "alice@example.com",
    "avatarColor": "#8E44AD"
  }
]
```

### 4.7 Add Group Member

**Purpose:** Add member to group.  

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.addMember` → `GroupService.addMember` → `GroupMemberRepository.save`


**Endpoint:** `/api/groups/{id}/members`  
**Method:** `POST`  
**Request Body:**

```json
{
  "userId": 2
}
```

**Response:** (200 OK)

```json
{
  "success": true,
  "message": "Member added to group",
  "data": {
    "groupId": 5,
    "userId": 2
  }
}
```

### 4.8 Remove Group Member

**Purpose:** Remove member from group.  

**Persistence (data layer)**

**Tables:** `group_members`
**Flow:** `GroupController.removeMember` → `GroupService.removeMember` → `GroupMemberRepository.deleteByGroupIdAndUserId`


**Endpoint:** `/api/groups/{id}/members/{userId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 5) Sprint APIs

Base: `/api/spaces/{spaceId}/sprints`

### 5.1 Get Sprints By Space

**Purpose:** List sprints in a space.  

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.getBySpace` → `SprintService.findBySpace` → `SprintRepository.findBySpaceIdOrderByStartDateAsc`


**Endpoint:** `/api/spaces/{spaceId}/sprints`  
**Method:** `GET`  
**Response:** (200 OK) `SprintDto[]`

```json
[
  {
    "id": 101,
    "spaceId": 10,
    "name": "Sprint 25",
    "goal": "Finish ticket-detail stabilization",
    "startDate": "2026-04-15",
    "endDate": "2026-04-28",
    "status": "PLANNED"
  }
]
```

### 5.2 Get Sprint By ID

**Purpose:** Get one sprint.  

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.getById` → `SprintService.findById` → `SprintRepository.findById`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `GET`  
**Response:** (200 OK) `SprintDto`

```json
{
  "id": 101,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail stabilization",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

### 5.3 Create Sprint

**Purpose:** Create sprint.  

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.create` → `SprintService.create` → `SprintRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/sprints`  
**Method:** `POST`  
**Request Body:** (`CreateSprintRequest`)

```json
{
  "name": "Sprint 25",
  "goal": "Finish ticket-detail stabilization",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

**Response:** (200 OK) `SprintDto`

```json
{
  "id": 102,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail stabilization",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "PLANNED"
}
```

### 5.4 Update Sprint

**Purpose:** Update sprint.  

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.update` → `SprintService.update` → `SprintRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `PUT`  
**Request Body:** (`CreateSprintRequest`)  
**Response:** (200 OK) `SprintDto`

```json
{
  "id": 102,
  "spaceId": 10,
  "name": "Sprint 25",
  "goal": "Finish ticket-detail stabilization",
  "startDate": "2026-04-15",
  "endDate": "2026-04-28",
  "status": "ACTIVE"
}
```

### 5.5 Delete Sprint

**Purpose:** Delete sprint.  

**Persistence (data layer)**

**Tables:** `sprints`
**Flow:** `SprintController.delete` → `SprintService.delete` → `SprintRepository.deleteById`


**Endpoint:** `/api/spaces/{spaceId}/sprints/{id}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 6) Issue APIs

Base: `/api/spaces/{spaceId}/issues`

### 6.1 Get Issues By Space

**Purpose:** List issues in space.  

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.getBySpace` → `IssueService.findBySpace` → `IssueRepository.findBySpaceIdOrderByIssueOrderAsc`


**Endpoint:** `/api/spaces/{spaceId}/issues`  
**Method:** `GET`  
**Response:** (200 OK) `IssueDto[]`

```json
[
  {
    "id": 1001,
    "issueKey": "PLT-42",
    "spaceId": 10,
    "title": "Fix drag order mismatch",
    "status": "IN_PROGRESS",
    "priority": "HIGH"
  }
]
```

### 6.2 Get Issue By Key

**Purpose:** Get issue by business key.  

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.getByKey` → `IssueService.findByKey` → `IssueRepository.findByIssueKey`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `GET`  
**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "description": "<p>Repro in board view</p>",
  "status": "IN_PROGRESS",
  "priority": "HIGH"
}
```

### 6.3 Create Issue

**Purpose:** Create issue in space.  

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.create` → `IssueService.create` → `IssueRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/issues`  
**Method:** `POST`  
**Request Body:** (`CreateIssueRequest`)

```json
{
  "title": "Fix drag order mismatch",
  "description": "<p>Repro in board view</p>",
  "issueType": "BUG",
  "status": "TODO",
  "priority": "HIGH",
  "assigneeId": 2,
  "reporterId": 1,
  "sprintId": 101,
  "parentId": null,
  "storyPoints": 3,
  "startDate": "2026-04-29",
  "dueDate": "2026-05-02",
  "labels": ["board", "frontend"]
}
```

**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "status": "TODO",
  "priority": "HIGH"
}
```

### 6.4 Update Issue

**Purpose:** Update issue fields (supports clear flags).  

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.update` → `IssueService.update` → `IssueRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `PUT`  
**Request Body:** (`UpdateIssueRequest`)

```json
{
  "status": "IN_PROGRESS",
  "assigneeId": 2,
  "clearAssignee": false,
  "clearReporter": false,
  "sprintId": 101,
  "clearSprint": false,
  "parentId": null,
  "clearParent": true,
  "storyPoints": 5,
  "issueOrder": 120,
  "labels": ["board", "urgent"]
}
```

**Response:** (200 OK) `IssueDto`

```json
{
  "id": 1001,
  "issueKey": "PLT-42",
  "spaceId": 10,
  "title": "Fix drag order mismatch",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "labels": ["board", "urgent"]
}
```

### 6.5 Delete Issue

**Purpose:** Delete issue.  

**Persistence (data layer)**

**Tables:** `issues`
**Flow:** `IssueController.delete` → `IssueService.delete` → `IssueRepository.delete`


**Endpoint:** `/api/spaces/{spaceId}/issues/{issueKey}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 7) Comment APIs

Base: `/api/issues/{issueId}/comments`

### 7.1 Get Comments

**Purpose:** List comments for issue.  

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.getByIssue` → `CommentService.findByIssue` → `CommentRepository.findByIssueIdOrderByCreatedAtAsc`


**Endpoint:** `/api/issues/{issueId}/comments`  
**Method:** `GET`  
**Response:** (200 OK) `CommentDto[]`

```json
[
  {
    "id": 2001,
    "issueId": 1001,
    "authorId": 2,
    "content": "<p>I can reproduce this issue.</p>",
    "createdAt": "2026-04-29T20:00:00Z",
    "updatedAt": "2026-04-29T20:00:00Z"
  }
]
```

### 7.2 Create Comment

**Purpose:** Create comment.  

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.create` → `CommentService.create` → `CommentRepository.save`


**Endpoint:** `/api/issues/{issueId}/comments`  
**Method:** `POST`  
**Request Body:** (`CreateCommentRequest`)

```json
{
  "authorId": 2,
  "content": "<p>I can reproduce this issue.</p>"
}
```

**Response:** (200 OK) `CommentDto`

```json
{
  "id": 2001,
  "issueId": 1001,
  "authorId": 2,
  "content": "<p>I can reproduce this issue.</p>",
  "createdAt": "2026-04-29T20:00:00Z",
  "updatedAt": "2026-04-29T20:00:00Z"
}
```

### 7.3 Update Comment

**Purpose:** Update comment content.  

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.update` → `CommentService.update` → `CommentRepository.save`


**Endpoint:** `/api/issues/{issueId}/comments/{commentId}`  
**Method:** `PUT`  
**Request Body:**

```json
{
  "content": "<p>Root cause identified.</p>"
}
```

**Response:** (200 OK) `CommentDto`

```json
{
  "id": 2001,
  "issueId": 1001,
  "authorId": 2,
  "content": "<p>Root cause identified.</p>",
  "createdAt": "2026-04-29T20:00:00Z",
  "updatedAt": "2026-04-29T20:05:00Z"
}
```

### 7.4 Delete Comment

**Purpose:** Delete comment.  

**Persistence (data layer)**

**Tables:** `comments`
**Flow:** `CommentController.delete` → `CommentService.delete` → `CommentRepository.deleteById`


**Endpoint:** `/api/issues/{issueId}/comments/{commentId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 8) Attachment APIs

Base: `/api/issues/{issueId}/attachments`

### 8.1 Get Attachments

**Purpose:** List issue attachments.  

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.getByIssue` → `IssueAttachmentService.findByIssue` → `IssueAttachmentRepository.findByIssueIdOrderByCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/attachments`  
**Method:** `GET`  
**Response:** (200 OK) `IssueAttachmentDto[]`

```json
[
  {
    "id": 5001,
    "issueId": 1001,
    "originalFilename": "stacktrace.png",
    "contentType": "image/png",
    "sizeBytes": 245123,
    "createdAt": "2026-04-29T21:00:00Z"
  }
]
```

### 8.2 Upload Attachment

**Purpose:** Upload attachment to issue.  

**Side effects (optional):** When **`APP_KAFKA_ATTACHMENT_INGESTION_ENABLED=true`**, a Kafka tracking message may be published after the upload transaction commits (same behaviour as described under **§8.2 Upload Attachment** earlier in this document).

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.upload` → `IssueAttachmentService.upload` → `IssueAttachmentRepository.save`


**Endpoint:** `/api/issues/{issueId}/attachments`  
**Method:** `POST`  
**Content-Type:** `multipart/form-data`

**Form Data:**

- `file`: binary file (required)
- `embedded`: boolean (optional, default `false`)

**Response:** (200 OK) `IssueAttachmentDto`

```json
{
  "id": 5001,
  "issueId": 1001,
  "originalFilename": "stacktrace.png",
  "contentType": "image/png",
  "sizeBytes": 245123,
  "createdAt": "2026-04-29T21:00:00Z"
}
```

### 8.3 Download Attachment

**Purpose:** Download attachment content.  

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.download` → `IssueAttachmentService.download` → `IssueAttachmentRepository.findById`


**Endpoint:** `/api/issues/{issueId}/attachments/{attachmentId}/download`  
**Method:** `GET`  
**Response:** (200 OK, binary stream)

### 8.4 Delete Attachment

**Purpose:** Delete attachment.  

**Important:** Same behaviour as **§8.4 Delete Attachment** earlier — **no** automatic scrub of description/comment HTML; **no** Kafka delete event.

**Persistence (data layer)**

**Tables:** `issue_attachments`
**Flow:** `IssueAttachmentController.delete` → `IssueAttachmentService.delete` → `IssueAttachmentRepository.delete`


**Endpoint:** `/api/issues/{issueId}/attachments/{attachmentId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 9) Work Log APIs

Base: `/api/issues/{issueId}/worklogs`

### 9.1 Get Work Logs

**Purpose:** List work logs for issue.  

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.getByIssue` → `WorkLogService.findByIssue` → `WorkLogRepository.findByIssueIdOrderByLogDateDescCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/worklogs`  
**Method:** `GET`  
**Response:** (200 OK) `WorkLogDto[]`

```json
[
  {
    "id": 7001,
    "issueId": 1001,
    "authorId": 2,
    "spentMinutes": 90,
    "note": "Investigated sorting logic",
    "logDate": "2026-04-29"
  }
]
```

### 9.2 Create Work Log

**Purpose:** Create work log entry.  

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.create` → `WorkLogService.create` → `WorkLogRepository.save`


**Endpoint:** `/api/issues/{issueId}/worklogs`  
**Method:** `POST`  
**Request Body:** (`CreateWorkLogRequest`)

```json
{
  "authorId": 2,
  "spentMinutes": 90,
  "note": "Investigated sorting logic",
  "logDate": "2026-04-29"
}
```

**Response:** (200 OK) `WorkLogDto`

```json
{
  "id": 7001,
  "issueId": 1001,
  "authorId": 2,
  "spentMinutes": 90,
  "note": "Investigated sorting logic",
  "logDate": "2026-04-29"
}
```

### 9.3 Update Work Log

**Purpose:** Update work log entry.  

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.update` → `WorkLogService.update` → `WorkLogRepository.save`


**Endpoint:** `/api/issues/{issueId}/worklogs/{workLogId}`  
**Method:** `PUT`  
**Request Body:** (`UpdateWorkLogRequest`)

```json
{
  "spentMinutes": 120,
  "note": "Added tests",
  "logDate": "2026-04-29"
}
```

**Response:** (200 OK) `WorkLogDto`

```json
{
  "id": 7001,
  "issueId": 1001,
  "authorId": 2,
  "spentMinutes": 120,
  "note": "Added tests",
  "logDate": "2026-04-29"
}
```

### 9.4 Delete Work Log

**Purpose:** Delete work log entry.  

**Persistence (data layer)**

**Tables:** `work_logs`
**Flow:** `WorkLogController.delete` → `WorkLogService.delete` → `WorkLogRepository.deleteById`


**Endpoint:** `/api/issues/{issueId}/worklogs/{workLogId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 10) Issue History APIs

### 10.1 Get Issue History

**Purpose:** Retrieve issue change history timeline.


**Persistence (data layer)**

**Tables:** `issue_history`
**Flow:** `IssueHistoryController.getByIssue` → `IssueHistoryService.findByIssue` → `IssueHistoryRepository.findByIssueIdOrderByCreatedAtDesc`


**Endpoint:** `/api/issues/{issueId}/history`  
**Method:** `GET`

**Response:** (200 OK)

```json
[
  {
    "id": 3001,
    "issueId": 1001,
    "eventType": "status_changed",
    "fieldName": "status",
    "fromValue": "TODO",
    "toValue": "IN_PROGRESS",
    "actorId": 2,
    "actorName": "Alice",
    "createdAt": "2026-04-29T20:00:00Z"
  }
]
```

---

## 11) Issue Link APIs

Base: `/api/issues/{issueId}/links`

### 11.1 Get Issue Links

**Purpose:** List links for issue.  

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.getByIssue` → `IssueLinkService.findByIssue` → `IssueLinkRepository.findBySourceIssueIdOrTargetIssueId`


**Endpoint:** `/api/issues/{issueId}/links`  
**Method:** `GET`  
**Response:** (200 OK) `IssueLinkDto[]`

```json
[
  {
    "id": 801,
    "relation": "blocks",
    "linkedIssueId": 1002,
    "linkedIssueKey": "PLT-43",
    "linkedIssueTitle": "Refactor drag handlers",
    "createdAt": "2026-04-29T20:30:00Z"
  }
]
```

### 11.2 Create Issue Link

**Purpose:** Create relation to another issue.  

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.create` → `IssueLinkService.create` → `IssueLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/links`  
**Method:** `POST`  
**Request Body:** (`CreateIssueLinkRequest`)

```json
{
  "linkedIssueId": 1002,
  "relation": "blocks"
}
```

**Response:** (200 OK) `IssueLinkDto`

```json
{
  "id": 801,
  "relation": "blocks",
  "linkedIssueId": 1002,
  "linkedIssueKey": "PLT-43",
  "linkedIssueTitle": "Refactor drag handlers",
  "createdAt": "2026-04-29T20:30:00Z"
}
```

### 11.3 Delete Issue Link

**Purpose:** Delete issue relation link.  

**Persistence (data layer)**

**Tables:** `issue_links`
**Flow:** `IssueLinkController.delete` → `IssueLinkService.delete` → `IssueLinkRepository.delete`


**Endpoint:** `/api/issues/{issueId}/links/{linkId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

---

## 12) Code Link / GitHub Integration APIs

### 12.1 Get Issue Code Links

**Purpose:** List code links bound to issue.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.getByIssue` → `IssueCodeLinkService.findByIssue` → `IssueCodeLinkRepository.findByIssueIdOrderByActivityDesc`


**Endpoint:** `/api/issues/{issueId}/code-links`  
**Method:** `GET`  
**Response:** (200 OK) `IssueCodeLinkDto[]`

```json
[
  {
    "id": 9001,
    "issueId": 1001,
    "url": "https://github.com/org/repo/pull/123",
    "provider": "github",
    "kind": "pull_request",
    "owner": "org",
    "repo": "repo",
    "refId": "123",
    "title": "Fix drag drop order",
    "state": "open",
    "authorLogin": "alice",
    "lastActivityAt": "2026-04-29T23:00:00Z"
  }
]
```

### 12.2 Create Issue Code Link

**Purpose:** Create code link for issue.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.create` → `IssueCodeLinkService.create` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/code-links`  
**Method:** `POST`  
**Request Body:** (`CreateIssueCodeLinkRequest`)

```json
{
  "url": "https://github.com/org/repo/pull/123",
  "linkType": "PR"
}
```

**Response:** (200 OK) `IssueCodeLinkDto`

```json
{
  "id": 9002,
  "issueId": 1001,
  "url": "https://github.com/org/repo/pull/124",
  "provider": "github",
  "kind": "pull_request",
  "owner": "org",
  "repo": "repo",
  "refId": "124",
  "title": "Improve board reorder stability",
  "state": "open",
  "authorLogin": "bob",
  "lastActivityAt": "2026-04-30T09:10:00Z"
}
```

### 12.3 Delete Issue Code Link

**Purpose:** Delete code link from issue.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.delete` → `IssueCodeLinkService.delete` → `IssueCodeLinkRepository.delete`


**Endpoint:** `/api/issues/{issueId}/code-links/{linkId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

### 12.4 Get Space Code Links

**Purpose:** List code links in a space.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.getBySpace` → `IssueCodeLinkService.findBySpace` → `IssueCodeLinkRepository.findByIssueSpaceIdOrderByActivityDesc`


**Endpoint:** `/api/spaces/{spaceId}/code-links`  
**Method:** `GET`  
**Response:** (200 OK) `IssueCodeLinkDto[]`

```json
[
  {
    "id": 9001,
    "issueId": 1001,
    "url": "https://github.com/org/repo/pull/123",
    "provider": "github",
    "kind": "pull_request",
    "owner": "org",
    "repo": "repo",
    "refId": "123",
    "title": "Fix drag drop order",
    "state": "open",
    "authorLogin": "alice",
    "lastActivityAt": "2026-04-29T23:00:00Z"
  }
]
```

### 12.5 Refresh Space Code Links

**Purpose:** Refresh code-link metadata in space.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshSpace` → `IssueCodeLinkService.refreshSpace` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/code-links/refresh`  
**Method:** `POST`  
**Request Body:** (`GithubTokenRequest`, optional)

```json
{
  "githubToken": "github_pat_xxx"
}
```

**Response:** (200 OK) refresh result object

```json
{
  "checked": 12,
  "updated": 9
}
```

### 12.6 Refresh Issue Code Links

**Purpose:** Refresh code-link metadata in one issue.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshIssue` → `IssueCodeLinkService.refreshIssue` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/issues/{issueId}/code-links/refresh`  
**Method:** `POST`  
**Request Body:** optional `GithubTokenRequest`  
**Response:** (200 OK) refresh result object

```json
{
  "checked": 4,
  "updated": 3
}
```

### 12.7 Refresh One Code Link

**Purpose:** Refresh one code-link metadata.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `IssueCodeLinkController.refreshOne` → `IssueCodeLinkService.refreshOne` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/code-links/{linkId}/refresh`  
**Method:** `POST`  
**Request Body:** optional `GithubTokenRequest`  
**Response:** (200 OK) `IssueCodeLinkDto`

```json
{
  "id": 9001,
  "issueId": 1001,
  "url": "https://github.com/org/repo/pull/123",
  "provider": "github",
  "kind": "pull_request",
  "owner": "org",
  "repo": "repo",
  "refId": "123",
  "title": "Fix drag drop order",
  "state": "closed",
  "authorLogin": "alice",
  "lastActivityAt": "2026-04-30T10:20:00Z"
}
```

### 12.8 List Space GitHub Repositories

**Purpose:** List configured GitHub repos in space.  

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.list` → `GithubScanService.listRepos` → `SpaceGithubRepoRepository.findBySpaceIdOrderByCreatedAtAsc`


**Endpoint:** `/api/spaces/{spaceId}/github-repos`  
**Method:** `GET`  
**Response:** (200 OK) `SpaceGithubRepoDto[]`

```json
[
  {
    "id": 1,
    "spaceId": 10,
    "owner": "my-org",
    "repo": "my-repo",
    "createdAt": "2026-04-28T10:00:00Z",
    "lastScannedAt": null
  }
]
```

### 12.9 Add Space GitHub Repository

**Purpose:** Add one GitHub repo to space.  

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.add` → `GithubScanService.addRepo` → `SpaceGithubRepoRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos`  
**Method:** `POST`  
**Request Body:** (`CreateSpaceGithubRepoRequest`)

```json
{
  "owner": "my-org",
  "repo": "my-repo",
  "defaultBranch": "main"
}
```

**Response:** (200 OK) `SpaceGithubRepoDto`

```json
{
  "id": 1,
  "spaceId": 10,
  "owner": "my-org",
  "repo": "my-repo",
  "createdAt": "2026-04-28T10:00:00Z",
  "lastScannedAt": null
}
```

### 12.10 Bulk Import GitHub Repositories

**Purpose:** Bulk import repos from account/org.  

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.bulkImport` → `GithubScanService.bulkAddReposFromAccount` → `SpaceGithubRepoRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/bulk`  
**Method:** `POST`  
**Request Body:** `BulkImportGithubReposRequest`  
**Response:** (200 OK) `BulkImportGithubReposResult`

```json
{
  "discovered": 25,
  "added": 18,
  "skipped": 7
}
```

### 12.11 Remove Space GitHub Repository

**Purpose:** Remove repo from space.  

**Persistence (data layer)**

**Tables:** `space_github_repos`
**Flow:** `SpaceGithubRepoController.remove` → `GithubScanService.removeRepo` → `SpaceGithubRepoRepository.delete`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/{repoId}`  
**Method:** `DELETE`  
**Response:** (204 No Content)

### 12.12 Scan Space GitHub Repositories

**Purpose:** Scan configured repos and sync issue references.  

**Persistence (data layer)**

**Tables:** `issue_code_links`
**Flow:** `SpaceGithubRepoController.scan` → `GithubScanService.scanSpace` → `IssueCodeLinkRepository.save`


**Endpoint:** `/api/spaces/{spaceId}/github-repos/scan`  
**Method:** `POST`  
**Request Body:** optional `GithubTokenRequest`  
**Response:** (200 OK) scan result object

```json
{
  "reposScanned": 4,
  "reposRemoved": 0,
  "prsInspected": 120,
  "commitsInspected": 650,
  "linksCreated": 14,
  "perRepo": [],
  "warnings": []
}
```

---

## Error Response Pattern

Current project often returns:

```json
{
  "message": "Error detail"
}
```

Typical status codes:

- `400 Bad Request`
- `401 Unauthorized`
- `403 Forbidden`
- `404 Not Found`
- `409 Conflict`
- `500 Internal Server Error`

