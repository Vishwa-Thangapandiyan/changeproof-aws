# ChangeProof

## Project Concept

> **Instead of asking whether a cloud infrastructure change *looks* safe, ChangeProof lets you test it before production.**

ChangeProof is an AWS-native infrastructure change verification system.

It takes a proposed infrastructure change, predicts its possible impact, creates an isolated test environment, applies the change there, generates representative workload, observes what actually happens, and compares the prediction against the real outcome.

The system can therefore answer:

- **Before deployment:** "What could this change affect?"
- **Before production:** "Can we experimentally verify that?"
- **After testing:** "What actually happened?"
- **Over time:** "Were our predictions correct?"

---

# 1. The Problem

Cloud applications are made of many interconnected components:

```text
                    YOUR APPLICATION
                          │
              ┌───────────┼───────────┐
              ↓           ↓           ↓
           Backend     Database      Storage
              │           │
              ↓           ↓
           Service A    Service B
```

A small infrastructure change can therefore affect things that the developer did not directly modify.

For example:

- changing an IAM permission can remove access to a database;
- changing a security-group rule can expose a service;
- increasing Lambda concurrency can overload a database;
- changing networking can increase latency or AWS costs;
- changing one service can cause failures in downstream services.

Today, engineers often rely on code review, static IaC scanners, documentation, dashboards, tribal knowledge, and staging environments to understand these consequences.

The fundamental question remains:

> **"What will actually happen if I make this change?"**

---

# 2. The Core Idea

ChangeProof combines:

```text
                PROPOSED CHANGE
                       │
                       ↓
              STATIC ANALYSIS
                       │
                       ↓
              LIVE DEPENDENCY
                   GRAPH
                       │
                       ↓
              IMPACT PREDICTION
                       │
                       ↓
             ISOLATED TEST ENV
                       │
                       ↓
               REAL WORKLOAD
                       │
                       ↓
                OBSERVE AWS
                       │
                       ↓
              ACTUAL OUTCOME
                       │
                       ↓
          PREDICTED vs ACTUAL
                       │
                       ↓
               SAFE DECISION
```

The key differentiator is:

> **Don't just review the change. Verify it.**

---

# 3. What Is the Dependency Graph?

AWS infrastructure contains many relationships.

For example:

```text
                 API
                  │
          ┌───────┴───────┐
          ↓               ↓
       Lambda            S3
          │
          ↓
         RDS
          │
          ↓
       Backup
```

The graph represents:

- which services communicate;
- which resources depend on each other;
- which identities have access to which resources;
- which applications depend on which databases;
- which components can be affected by a change.

Each box is a resource or system component.

Each connection means:

> **"This component depends on or interacts with that component."**

---

# 4. AWS Data Sources

ChangeProof can collect information from AWS services that provide different views of the environment.

## AWS Config

Think:

> **"What does my infrastructure look like?"**

It provides information about AWS resources and their configurations.

---

## AWS CloudTrail

Think:

> **"Who changed what, and when?"**

Example:

```text
14:32
IAM permission changed

14:35
Security group modified

14:41
Database configuration changed
```

CloudTrail provides an audit history of AWS API activity.

---

## Amazon CloudWatch

Think:

> **"How is the system behaving?"**

Examples:

```text
CPU
Memory
Latency
Errors
Invocations
Throttles
Queue depth
Database connections
```

This is the main source for observing the actual result of a test.

---

## AWS X-Ray / OpenTelemetry

Think:

> **"How does a request travel through the application?"**

Example:

```text
User request
     ↓
API
     ↓
Lambda
     ↓
Database
     ↓
Response
```

This helps discover application-level dependencies and performance bottlenecks.

---

## IAM

Think:

> **"Who or what is allowed to do what?"**

Example:

```text
Backend service

Allowed:
    Read Database
    Write Storage

Not allowed:
    Delete Database
```

IAM relationships are important for security impact analysis.

---

# 5. What Is "Blast Radius"?

Blast radius means:

> **How much of the system could potentially be affected by a change?**

### Small blast radius

```text
Service A
    ↓
Service B
```

A problem in A may only affect B.

### Large blast radius

```text
                    API
                  /  |  \
                 ↓   ↓   ↓
                DB  S3  Queue
                │       │
                ↓       ↓
             Payments  Workers
```

A change to the API could potentially affect:

- the database;
- storage;
- payments;
- workers.

ChangeProof estimates this impact before deployment.

---

# 6. The Impact Engine

The Impact Engine evaluates a proposed change across multiple dimensions.

## Security

Questions such as:

- Does this create a dangerous permission?
- Does this expose a resource?
- Does this create a new attack path?
- Does an identity gain unnecessary access?

## Reliability

Questions such as:

- Could a downstream service fail?
- Could traffic overload a dependency?
- Could latency increase?
- Could queues or connections become saturated?

## Cost

Questions such as:

- Could this increase resource usage?
- Could traffic increase?
- Could the change trigger additional compute?
- Could it increase data-transfer costs?

Example output:

```text
SECURITY
    HIGH

RELIABILITY
    MEDIUM

COST
    LOW

BLAST RADIUS
    7 resources

CONFIDENCE
    89%
```

---

# 7. The Important Part: Verification

A prediction alone is not enough.

ChangeProof creates an isolated test environment.

Instead of:

```text
Developer
    ↓
Change
    ↓
Production
```

it does:

```text
Developer
    ↓
Proposed change
    ↓
Terraform
    ↓
Temporary AWS environment
    ↓
Apply change
    ↓
Run workload
    ↓
Observe
    ↓
Destroy environment
```

For example:

```text
PRODUCTION

VPC
 ├── API
 ├── Lambda
 └── RDS


TEST ENVIRONMENT

VPC-test
 ├── API-test
 ├── Lambda-test
 └── RDS-test
```

Terraform can create and destroy this environment.

---

# 8. Synthetic Workload

The isolated environment needs realistic activity.

For example, if the change is:

```text
Lambda concurrency:
10 → 100
```

ChangeProof can generate representative traffic:

```text
100 requests/sec
```

Then measure:

```text
CPU
Latency
Errors
Database connections
Queue depth
Lambda throttles
```

This turns a theoretical prediction into an experiment.

---

# 9. Predicted vs Actual

Suppose ChangeProof predicts:

```text
Database latency:
Expected increase ≈ 20%
```

The test environment produces:

```text
Before:
80 ms

After:
1,200 ms
```

The system records:

```text
PREDICTED
    +20%

ACTUAL
    +1,400%

RESULT
    ❌ Reliability regression
```

This means the change should not be promoted to production.

---

# 10. The Feedback Loop

This is one of the most important parts of the project.

```text
                 PREDICTION
                     │
                     ↓
                  DEPLOY
                     │
                     ↓
               ACTUAL RESULT
                     │
                     ↓
           COMPARE PREDICTION
               WITH REALITY
                     │
                     ↓
               STORE RESULT
                     │
                     ↓
          IMPROVE FUTURE ANALYSIS
```

For example:

```text
Prediction:
RDS latency +20%

Actual:
RDS latency +18%

→ Prediction was close
```

Or:

```text
Prediction:
Low reliability risk

Actual:
Database overloaded

→ Prediction was wrong
```

The accumulated history can then be used to improve future predictions.

The first version does not need a sophisticated ML model. It can begin with:

```text
Rules
+
Dependency analysis
+
Historical observations
```

Machine learning can be added later.

---

# 11. Backward Mode

The system can eventually operate in the opposite direction.

Instead of asking:

> **"What will this change break?"**

it can ask:

> **"What caused this incident?"**

Example:

```text
14:00
Deployment

14:03
Database latency increases

14:05
API errors increase

14:07
Payment failures
```

ChangeProof reconstructs the chain:

```text
Payment failure
      ↑
API errors
      ↑
Database latency
      ↑
Deployment
```

The goal is to identify the most likely initiating event using infrastructure relationships and telemetry.

This becomes the incident/root-cause analysis mode.

---

# 12. AI's Role

AI should not be responsible for the core prediction.

The core system should produce evidence first:

```text
Change
   ↓
Graph analysis
   ↓
Telemetry
   ↓
Measured results
```

Then an LLM can explain those results.

For example:

> **Why was this change rejected?**

The underlying engine provides:

```text
Change:
Lambda concurrency 10 → 100

Observed:
RDS connections +82%
Latency +310%
Queue depth +460%

Dependency:
Lambda → SQS → RDS
```

Amazon Bedrock can turn that into a human-readable explanation:

> Increasing Lambda concurrency allowed substantially more simultaneous database requests. During the isolated test, RDS connections increased by 82%, resulting in a 310% increase in latency and significant queue growth. The change therefore failed the reliability threshold.

The LLM explains the evidence.

It does not invent the evidence.

---

# 13. Proposed AWS Architecture

```text
                         GitHub PR
                            │
                            ↓
                       API Gateway
                            │
                            ↓
                     AWS Lambda
                            │
                            ↓
                     Step Functions
                            │
              ┌─────────────┴─────────────┐
              ↓                           ↓
       Terraform Plan              Dependency Graph
              │                           │
              ↓                           ↓
       Change Parser                Amazon Neptune
              │                           │
              └─────────────┬─────────────┘
                            ↓
                     Impact Engine
                         Python
                            │
                ┌───────────┼───────────┐
                ↓           ↓           ↓
             Security   Reliability    Cost
                │           │           │
                └───────────┼───────────┘
                            ↓
                    Prediction Result
                            │
                            ↓
                Temporary AWS Environment
                            │
                     Terraform Apply
                            │
                            ↓
                  Synthetic Workload
                            │
              ┌─────────────┼─────────────┐
              ↓             ↓             ↓
         CloudWatch       X-Ray       CloudTrail
              │             │             │
              └─────────────┼─────────────┘
                            ↓
                     Actual Outcome
                            │
                            ↓
                     Compare Engine
                            │
                  ┌─────────┴─────────┐
                  ↓                   ↓
               APPROVE              REJECT
                  │                   │
                  └─────────┬─────────┘
                            ↓
                       Historical Data
                            │
                            ↓
                       S3 / DynamoDB
                            │
                            ↓
                       Future Analysis
```

---

# 14. Recommended Technology Stack

## Frontend

- React
- Vite
- AWS Amplify or S3 + CloudFront

## Backend

- Python
- FastAPI where a conventional API is useful
- AWS Lambda for event-driven functions

## Infrastructure

- Terraform

Terraform is central because the system needs to understand and test infrastructure changes.

## AWS Services

| Purpose | AWS Service |
|---|---|
| API | API Gateway |
| Workflow orchestration | Step Functions |
| Serverless compute | Lambda |
| Infrastructure | Terraform |
| Dependency graph | Amazon Neptune |
| Metrics/logs | CloudWatch |
| Distributed tracing | X-Ray / OpenTelemetry |
| Audit history | CloudTrail |
| Object storage | S3 |
| Application state | DynamoDB |
| AI explanation | Amazon Bedrock |
| ML | SageMaker (optional) |
| Authentication | Cognito |
| Permissions | IAM |
| Notifications | SNS |
| Search/log analysis | OpenSearch (optional) |

---

# 15. MVP Scope

Do **not** try to build the entire architecture during the hackathon.

The minimum impressive vertical slice should be:

```text
GitHub PR
    ↓
Terraform plan
    ↓
Change parser
    ↓
Dependency graph
    ↓
Impact prediction
    ↓
Temporary AWS environment
    ↓
Synthetic workload
    ↓
CloudWatch
    ↓
Actual result
    ↓
Predicted vs Actual
    ↓
PASS / FAIL
```

Then use Bedrock to explain the result.

One change type working end-to-end is more valuable than ten partially implemented features.

---

# 16. Recommended First Demo Change

A good initial change is:

```text
Lambda concurrency:
10 → 100
```

Why?

Because it is easy to demonstrate a measurable reliability effect.

The system can show:

```text
BEFORE

Lambda concurrency: 10
Database latency: 80ms
RDS connections: 30%


PROPOSED CHANGE

Lambda concurrency: 100


PREDICTION

Reliability: MEDIUM
Potential RDS overload


VERIFICATION

Synthetic workload
        ↓
Lambda concurrency ↑
        ↓
RDS connections ↑
        ↓
Latency ↑
        ↓
Queue depth ↑


RESULT

❌ Reliability regression
```

---

# 17. Three-Minute Demo

### 0:00 — Healthy application

Show:

```text
✓ API
✓ Database
✓ Queue
```

### 0:20 — Submit infrastructure change

```text
Lambda concurrency:
10 → 100
```

### 0:40 — Prediction

```text
Security       🟢
Reliability    🟡
Cost           🟡

Blast radius:
Lambda → SQS → RDS
```

### 1:00 — Verify

Click:

> **VERIFY CHANGE**

ChangeProof creates the isolated environment.

### 1:20 — Generate workload

Run synthetic traffic.

### 1:40 — Observe

```text
RDS connections: +82%
Latency: +310%
Queue depth: +460%
```

### 2:00 — Decision

```text
❌ CHANGE REJECTED

Reason:
Reliability regression detected
```

### 2:20 — Explain

Bedrock produces an evidence-grounded explanation.

### 2:40 — Feedback

Show:

```text
PREDICTED vs ACTUAL

Prediction:
Moderate impact

Actual:
Severe database saturation
```

Store the result.

### 3:00 — Final message

> **"Instead of asking whether a cloud change looks safe, ChangeProof lets you test it before production."**

---

# 18. The Core Differentiator

ChangeProof should **not** be positioned as:

> "An AI that reviews Terraform."

Existing AI coding/IaC tools can already review infrastructure code and identify many potential problems.

It should instead be positioned as:

> **"A verification system that connects infrastructure changes to the actual live architecture, experimentally tests those changes in an isolated environment, observes their real behavior, and learns from predicted-versus-actual outcomes."**

The distinction is:

```text
AI CODE REVIEWER

Code
 ↓
Analysis
 ↓
Suggestion


CHANGEPROOF

Infrastructure Change
 ↓
Understand Environment
 ↓
Predict Impact
 ↓
Build Test Environment
 ↓
Actually Test
 ↓
Observe
 ↓
Compare
 ↓
Approve / Reject
```

The key phrase:

# **Don't just review the change. Prove it.**
