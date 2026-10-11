
---
name: full-development
description: Use for substantial new software projects, modules, features or end-to-end changes that require requirements analysis, architecture, implementation, testing, review and release preparation.
---

# Full Development Workflow

## Stage 1: Intake
Read the user's prompt and all accessible reference files.

Extract:
- Functional requirements
- Non-functional requirements
- Acceptance criteria
- Constraints
- Assumptions
- Open questions

Do not fabricate unreadable or inaccessible content.

## Stage 2: Architecture
For substantial changes, delegate architecture review
to the architect agent.

Produce:
- System architecture
- Module boundaries
- API and data contracts
- Data model
- Technology decisions
- Security concerns
- Testing strategy

## Stage 3: Plan
Decompose the work into bounded tasks.

Each task must have:
- Owner or agent role
- Input and dependencies
- File scope
- Acceptance criteria
- Verification requirements

Identify which tasks may safely run concurrently.

## Stage 4: Implement
Delegate to the relevant agents when supported.

Possible specialists:
- frontend
- backend
- database
- security
- devops
- documentation

Keep tasks focused and coordinate shared contracts.

## Stage 5: Test
Use the qa agent for meaningful independent verification.

Perform applicable:
- Unit testing
- Integration testing
- API testing
- UI testing
- End-to-end testing
- Regression testing
- Error-path testing

## Stage 6: Review
Request independent code review.

Classify defects as:
- BLOCKER
- HIGH
- MEDIUM
- LOW

Resolve blocker and high-severity correctness issues
before calling the task complete.

## Stage 7: Verify
Run the real project verification commands.

Do not fabricate test results.
Report checks that could not run.

## Stage 8: Release Readiness
Where applicable, use devops and security agents.

Review:
- Build artifacts
- Configuration
- Secrets
- Migrations
- Monitoring
- Rollback
- Deployment approval

Do not deploy to production without explicit authorization.

## Stage 9: Final Report
Provide:
- Requirements implemented
- Architecture decisions
- Work delegated
- Files changed
- Verification evidence
- Outstanding risks
- Deployment readiness
- User actions required
