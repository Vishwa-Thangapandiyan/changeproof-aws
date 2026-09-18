# ChangeProof — AWS Services Explained

## The big picture

**ChangeProof** is an AWS-native system for answering:

> **“We are about to change our infrastructure. What could this change break, and can we prove the impact before production?”**

The basic flow is:

```text
Developer proposes Terraform change
            ↓
        API Gateway
            ↓
          Lambda
            ↓
      Step Functions
            ↓
 ┌─────────────────────────┐
 │ 1. Parse the change     │
 │ 2. Inspect dependencies │
 │ 3. Predict blast radius │
 │ 4. Create test env      │
 │ 5. Apply the change     │
 │ 6. Generate workload    │
 │ 7. Observe behavior     │
 │ 8. Compare prediction   │
 │ 9. Approve / Reject     │
 └─────────────────────────┘
            ↓
     Evidence + Report
            ↓
        Bedrock explains
```

The important idea is:

> **Don't just review the change. Prove it.**

---

# 1. API Gateway

## What is API Gateway?

**Amazon API Gateway** is the front door for applications that communicate through HTTP APIs.

Imagine your backend is a building. Instead of letting everyone directly walk into the building, you put a controlled entrance at the front.

That entrance is API Gateway.

For example:

```text
POST /changes
GET  /changes/123
GET  /results/123
```

A frontend, GitHub webhook, CI/CD pipeline, or another application can send requests to these endpoints.

API Gateway can handle things such as:

- Receiving HTTP requests
- Routing requests
- Authentication/authorization integration
- Request throttling
- Connecting requests to backend services

## What does it do in ChangeProof?

A developer might submit:

```text
Terraform change:
aws_lambda_function.api
reserved_concurrency:
10 → 100
```

The request could arrive through:

```text
GitHub / Dashboard
       ↓
API Gateway
       ↓
Lambda
```

API Gateway therefore acts as the **entry point into ChangeProof**.

### Simple mental model

> **API Gateway = front door**

---

# 2. AWS Lambda

## What is Lambda?

**AWS Lambda** is serverless compute.

Normally, if you want to run a program, you need a machine:

```text
Server
 └── Your program
```

With Lambda, AWS runs your function when an event happens.

You provide the code, and AWS handles much of the server management.

A Lambda function might look conceptually like:

```python
def handler(event, context):
    change = event["change"]

    # analyze change

    return {
        "status": "accepted"
    }
```

You don't need to keep a server running 24/7 just to execute that small piece of code.

## What does it do in ChangeProof?

Lambda can perform small pieces of the ChangeProof workflow.

For example:

### Parse a Terraform change

```text
Terraform plan
      ↓
Lambda
      ↓
Changed resources
```

### Query the dependency graph

```text
Lambda
   ↓
Neptune
   ↓
Related resources
```

### Calculate basic risk

```text
Changed RDS instance
      ↓
Find dependencies
      ↓
Identify affected services
```

Lambda is useful for these relatively short, event-driven tasks.

### Simple mental model

> **Lambda = small pieces of code that run when needed**

---

# 3. AWS Step Functions

## What is Step Functions?

**AWS Step Functions** is a workflow orchestration service.

This is extremely important for ChangeProof.

Suppose the system needs to perform:

```text
Parse Terraform
      ↓
Build impact graph
      ↓
Predict impact
      ↓
Create test environment
      ↓
Apply change
      ↓
Run workload
      ↓
Collect metrics
      ↓
Compare results
      ↓
Generate report
```

You could write one gigantic Lambda function.

That would be a poor architecture.

Instead, Step Functions coordinates the individual stages.

Conceptually:

```text
          START
            ↓
      Parse Terraform
            ↓
      Analyze Graph
            ↓
       Make Prediction
            ↓
      Create Test Env
            ↓
       Apply Change
            ↓
       Run Workload
            ↓
     Collect Telemetry
            ↓
     Compare Prediction
            ↓
      Generate Result
            ↓
           END
```

Step Functions can also handle:

- Retries
- Failures
- Waiting
- Branching
- Parallel execution
- Timeouts
- Workflow state

For example:

```text
Create environment
       ↓
   FAILED?
    /    \
  YES     NO
   ↓       ↓
Retry   Continue
```

## What does it do in ChangeProof?

It acts as the **orchestrator**.

It tells the other AWS services:

> “Do this first, then this, then this, and if something fails, handle it properly.”

### Simple mental model

> **Step Functions = workflow manager**

---

# 4. Amazon Neptune

## What is Neptune?

**Amazon Neptune** is a managed graph database.

This is one of the most important services for ChangeProof.

Traditional databases usually represent information like:

```text
Users
Orders
Products
```

A graph database is especially good when the **relationships between things** are important.

Imagine:

```text
ALB
 │
 ↓
Lambda
 │
 ↓
RDS
 │
 ↓
S3
```

Each object is a **node**.

Each relationship is an **edge**.

So:

```text
Lambda ──calls──> RDS
ALB ──routes──> Lambda
Lambda ──reads──> S3
```

Neptune is designed to work with this kind of highly connected data.

AWS supports graph query technologies including:

- Gremlin
- openCypher
- SPARQL

## What does it do in ChangeProof?

Suppose a developer changes:

```text
Security Group
```

ChangeProof needs to understand:

> What depends on this?

The graph could look like:

```text
Security Group
      ↓
     EC2
      ↓
Application
      ↓
     RDS
```

A change to the security group might therefore affect the EC2 instance and potentially the application path to RDS.

Neptune stores the environment's dependency relationships.

For example:

```text
[VPC]
  │
  ├── contains → [Subnet]
  │                 │
  │                 └── contains → [Lambda]
  │                                  │
  │                                  └── connects → [RDS]
  │
  └── contains → [Security Group]
```

Now ChangeProof can traverse the graph.

### Simple mental model

> **Neptune = what is connected to what?**

---

# 5. Amazon CloudWatch

## What is CloudWatch?

**Amazon CloudWatch** is AWS's monitoring and observability service.

It can collect things such as:

- Metrics
- Logs
- Alarms
- Application signals
- Resource performance information

Examples:

```text
CPU = 82%
Latency = 310 ms
Errors = 14/min
Lambda invocations = 20,000
RDS connections = 450
Queue depth = 2,300
```

These measurements tell you what is actually happening.

## What does it do in ChangeProof?

This is critical because ChangeProof isn't supposed to only predict.

It needs to **experiment and observe**.

Suppose ChangeProof predicts:

```text
Changing Lambda concurrency:
10 → 100

Expected:
Moderate RDS pressure
```

It creates the test environment and runs traffic.

CloudWatch might observe:

```text
Before:
RDS connections = 100

After:
RDS connections = 182
```

And:

```text
API latency:
Before = 120 ms
After  = 372 ms
```

And:

```text
Queue depth:
Before = 200
After  = 1,120
```

Now the system has actual evidence.

### Simple mental model

> **CloudWatch = what happened?**

---

# 6. AWS CloudTrail

## What is CloudTrail?

**AWS CloudTrail** records AWS API activity.

For example, someone might perform:

```text
CreateSecurityGroup
ModifyDBInstance
UpdateFunctionConfiguration
DeleteBucket
PutRolePolicy
```

CloudTrail helps answer:

```text
Who?
What?
When?
From where / under what identity?
```

For example:

```text
User: developer-role
Action: ModifyDBInstance
Time: 14:32
Resource: production-db
```

## What does it do in ChangeProof?

CloudTrail is useful for understanding **changes to the AWS environment**.

Imagine an incident happens:

```text
14:00 → deployment
14:03 → database latency increases
14:05 → errors increase
14:07 → service goes down
```

CloudTrail provides evidence of AWS API operations around that timeline.

It can therefore help correlate:

```text
Infrastructure change
       ↓
AWS API activity
       ↓
Telemetry changes
       ↓
Incident
```

This also supports ChangeProof's optional **backward mode**:

> “Something broke. What infrastructure change might have caused it?”

### Simple mental model

> **CloudTrail = what AWS changes were made, by whom, and when?**

---

# 7. AWS X-Ray / OpenTelemetry

## What is X-Ray?

**AWS X-Ray** provides distributed tracing.

Metrics tell you:

```text
Latency = 500 ms
```

But tracing can help answer:

> Where did those 500 ms come from?

Imagine a request:

```text
User
 ↓
API Gateway
 ↓
Lambda
 ↓
Service A
 ↓
RDS
```

A trace can show where time was spent.

For example:

```text
Total request: 500 ms

API Gateway     20 ms
Lambda           40 ms
Service A        80 ms
RDS             360 ms
```

Now you know that the database interaction is the major contributor.

## What does it do in ChangeProof?

Suppose the test environment shows:

```text
Latency increased by 250%
```

CloudWatch tells you:

> Latency increased.

Tracing helps tell you:

> The additional latency came primarily from RDS calls made by Lambda.

So ChangeProof can move from:

```text
Something got slower
```

to:

```text
This dependency became the bottleneck.
```

### Simple mental model

> **X-Ray / OpenTelemetry = where did the request go, and where did it spend time?**

---

# 8. Amazon S3

## What is S3?

**Amazon S3** is object storage.

You can think of it as a huge cloud storage system for files/objects.

Examples:

```text
terraform-plan.json
experiment-results.json
cloudwatch-export.json
trace-data.json
report.json
```

S3 is designed for storing large amounts of data.

## What does it do in ChangeProof?

ChangeProof generates evidence.

For example:

```text
Experiment #142

Terraform plan
Prediction
CloudWatch metrics
Traces
CloudTrail events
Final report
```

These artifacts can be stored in S3.

Example structure:

```text
s3://changeproof/
    experiments/
        142/
            terraform-plan.json
            prediction.json
            metrics.json
            traces.json
            report.json
```

This gives ChangeProof historical evidence.

### Simple mental model

> **S3 = long-term object/file storage**

---

# 9. Amazon DynamoDB

## What is DynamoDB?

**Amazon DynamoDB** is a managed NoSQL database.

It is useful for storing structured application data that needs fast access.

For example:

```text
Experiment ID
Status
Created time
Repository
Commit
Prediction
Actual result
Decision
```

A record might conceptually look like:

```json
{
  "experimentId": "EXP-142",
  "status": "REJECTED",
  "repository": "payment-api",
  "prediction": "moderate-risk",
  "actualImpact": "high",
  "decision": "reject"
}
```

## What does it do in ChangeProof?

DynamoDB can store the **state of ChangeProof's experiments**.

For example:

```text
EXP-142
    ↓
CREATING_ENVIRONMENT
    ↓
RUNNING_TEST
    ↓
COLLECTING_METRICS
    ↓
COMPLETED
```

The detailed files can live in S3 while DynamoDB stores the metadata/state needed by the application.

### Simple mental model

> **DynamoDB = fast structured application state**

---

# 10. Amazon Bedrock

## What is Bedrock?

**Amazon Bedrock** is AWS's managed platform for using foundation models.

It allows applications to use generative AI models through AWS-managed APIs.

Instead of building an LLM infrastructure stack yourself, your application can call a model through Bedrock.

## What does it do in ChangeProof?

This is where AI should be used carefully.

Bedrock should **explain the evidence**, rather than inventing the evidence.

For example, ChangeProof already knows:

```text
Lambda concurrency:
10 → 100

RDS connections:
100 → 182

Latency:
120 ms → 372 ms

Queue depth:
200 → 1120
```

Bedrock can turn that into:

> “The proposed concurrency increase caused significantly higher database connection pressure and increased request latency during the test workload. The observed behavior exceeded the predefined safety threshold.”

The AI is therefore acting as an **explanation layer**.

Not:

```text
LLM:
"I feel like this change is risky."
```

Instead:

```text
Measured evidence
       ↓
Bedrock
       ↓
Human-readable explanation
```

### Simple mental model

> **Bedrock = explain the evidence**

---

# 11. Amazon SageMaker

## What is SageMaker?

**Amazon SageMaker** is AWS's machine-learning platform.

It can be used for:

- Training models
- Deploying models
- Running inference
- Managing ML workflows
- Experiment tracking and related ML infrastructure

## What does it do in ChangeProof?

You **do not need SageMaker for the MVP**.

The first version can use deterministic rules.

For example:

```text
IF
RDS connections increase > 50%
AND
latency increases > 100%

THEN
high reliability risk
```

Later, after collecting hundreds or thousands of:

```text
Prediction
       +
Actual result
```

you could train a model.

For example:

```text
Historical changes
        ↓
Feature extraction
        ↓
ML model
        ↓
Predicted impact
```

SageMaker could host that model.

### Simple mental model

> **SageMaker = optional future ML engine**

---

# 12. IAM

## What is IAM?

**AWS Identity and Access Management (IAM)** controls:

> Who can do what to which AWS resources?

An IAM policy might conceptually say:

```text
Lambda:
    Can read S3

RDS:
    Cannot be modified

Production:
    No access

Test environment:
    Can create resources
```

This is extremely important for ChangeProof.

## What does it do in ChangeProof?

ChangeProof should **never give an AI agent unrestricted AWS access**.

A dangerous architecture would be:

```text
LLM
 ↓
Administrator permissions
 ↓
Entire AWS account
```

Instead:

```text
ChangeProof
    ↓
IAM role
    ↓
Only required permissions
    ↓
Temporary test environment
```

The system should be designed so that even if something goes wrong, production remains protected.

IAM can therefore enforce the boundary:

```text
┌──────────────────────────┐
│       PRODUCTION         │
│                          │
│      🚫 No test access   │
└──────────────────────────┘

┌──────────────────────────┐
│       TEST ACCOUNT       │
│                          │
│      ✅ ChangeProof      │
└──────────────────────────┘
```

### Simple mental model

> **IAM = who is allowed to do what**

---

# 13. Amazon Cognito

## What is Cognito?

**Amazon Cognito** provides authentication and user identity capabilities for applications.

For example:

```text
User
 ↓
Login
 ↓
Cognito
 ↓
Authenticated
 ↓
ChangeProof dashboard
```

## What does it do in ChangeProof?

If ChangeProof has a web dashboard, Cognito can handle user authentication.

For example:

```text
Developer
    ↓
Login
    ↓
Cognito
    ↓
Dashboard
```

You can then associate experiments with users or teams.

### Is it required?

Not necessarily.

For an early hackathon MVP, authentication can be simplified depending on the architecture.

### Simple mental model

> **Cognito = application login / user identity**

---

# 14. Amazon SNS

## What is SNS?

**Amazon Simple Notification Service (SNS)** is a messaging and notification service.

It can publish messages to subscribers.

For example:

```text
ChangeProof experiment finished
            ↓
           SNS
         /     \
        ↓       ↓
      Email   Other service
```

## What does it do in ChangeProof?

Suppose an experiment finishes:

```text
EXP-142
RESULT: REJECTED
```

SNS could trigger a notification:

```text
⚠️ ChangeProof verification failed

Change: Lambda concurrency 10 → 100

Observed:
RDS connections +82%
Latency +210%

Decision: REJECT
```

### Is it required?

No.

It's a useful extension for the MVP.

### Simple mental model

> **SNS = send notifications/messages**

---

# 15. Amazon OpenSearch

## What is OpenSearch?

**Amazon OpenSearch Service** is a managed search and analytics platform.

It is useful when you have large amounts of searchable information.

For example:

```text
CloudTrail events
Application logs
Security events
Experiment history
```

You might want to search:

```text
"Show all experiments involving RDS"
```

or:

```text
"Find changes that caused latency increases"
```

## What does it do in ChangeProof?

It could eventually provide a search layer over large amounts of historical evidence.

For example:

```text
Search:
"security group changes that caused outages"
```

and retrieve historical experiments.

### Is it required?

No.

It is an optional scaling feature.

### Simple mental model

> **OpenSearch = search and analyze lots of data**

---

# 16. Terraform

## Important: Terraform is NOT an AWS service

Terraform is an **Infrastructure as Code (IaC)** tool.

Instead of manually creating:

```text
VPC
Subnet
Lambda
RDS
Security Groups
```

you describe infrastructure as code.

For example:

```hcl
resource "aws_lambda_function" "api" {
    function_name = "api"
}
```

Terraform then creates/manages the desired infrastructure.

## Why is Terraform important to ChangeProof?

Terraform is the **input**.

Suppose the current infrastructure is:

```text
Lambda concurrency = 10
```

A developer proposes:

```text
Lambda concurrency = 100
```

Terraform can produce a plan describing the change.

Conceptually:

```text
CURRENT
10

PROPOSED
100

DIFF
10 → 100
```

ChangeProof analyzes that proposed change.

### Simple mental model

> **Terraform = what infrastructure do we want?**

---

# 17. React / Frontend

## What is React?

React is a frontend JavaScript library for building user interfaces.

For ChangeProof, you could build a dashboard such as:

```text
┌───────────────────────────────────────┐
│           ChangeProof                 │
├───────────────────────────────────────┤
│ Change: Lambda concurrency 10 → 100  │
│                                       │
│ Prediction:  Moderate Risk            │
│                                       │
│ Test Result: HIGH IMPACT              │
│                                       │
│ RDS Connections      +82%             │
│ API Latency          +210%            │
│ Queue Depth          +460%            │
│                                       │
│              ❌ REJECT                │
└───────────────────────────────────────┘
```

The React frontend communicates with the backend through API Gateway.

```text
React
  ↓
API Gateway
  ↓
Lambda
  ↓
ChangeProof backend
```

---

# 18. AWS Amplify

## What is Amplify?

**AWS Amplify** provides tooling for building and deploying web/mobile applications on AWS.

It can simplify parts of:

- Frontend deployment
- Authentication integration
- Backend integration
- Hosting

## What does it do in ChangeProof?

If you build the dashboard using React, Amplify can optionally help deploy/host it and integrate it with AWS services.

But it is not fundamental to the ChangeProof architecture.

### Simple mental model

> **Amplify = convenience layer for building/deploying applications on AWS**

---

# Putting everything together

The services now have different jobs.

```text
                    USER / GITHUB
                         │
                         ▼
                 ┌───────────────┐
                 │ API Gateway   │
                 └───────┬───────┘
                         │
                         ▼
                 ┌───────────────┐
                 │    Lambda     │
                 └───────┬───────┘
                         │
                         ▼
                 ┌────────────────┐
                 │ Step Functions │
                 └───────┬────────┘
                         │
          ┌──────────────┼───────────────┐
          │              │               │
          ▼              ▼               ▼
      Terraform       Neptune          IAM
      Analysis       Dependency       Permissions
                       Graph
          │              │
          └──────────────┼───────────────┘
                         │
                         ▼
                ┌─────────────────┐
                │  Test AWS Env   │
                └────────┬────────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
              ▼          ▼          ▼
         CloudWatch   X-Ray     CloudTrail
           Metrics     Traces      Events
              │          │          │
              └──────────┼──────────┘
                         │
                         ▼
                 Compare Results
                         │
                         ▼
                    S3 / DynamoDB
                         │
                         ▼
                     Bedrock
                         │
                         ▼
                  Human-readable
                     report
```

---

# The most important distinction

A common mistake is thinking all these AWS services are doing the same thing.

They aren't.

Each answers a different question.

| Service | Question it answers |
|---|---|
| **IAM** | Who is allowed to do what? |
| **Terraform** | What infrastructure is being proposed? |
| **Neptune** | What is connected to what? |
| **CloudTrail** | What AWS changes happened, and when? |
| **CloudWatch** | What happened to the system? |
| **X-Ray / OpenTelemetry** | Where did the request go / where was the bottleneck? |
| **Step Functions** | What should happen next? |
| **Lambda** | Execute this piece of code |
| **S3** | Where do we store large artifacts/evidence? |
| **DynamoDB** | Where do we store application state/metadata? |
| **Bedrock** | How do we explain the evidence? |
| **SageMaker** | How could we add a trained ML model later? |
| **Cognito** | Who is the application user? |
| **SNS** | Who should be notified? |
| **OpenSearch** | How do we search/analyze lots of historical data? |
| **React** | How does the user interact with the system? |
| **Amplify** | How can we simplify frontend deployment/integration? |

---

# What is actually needed for the MVP?

Don't try to build all of this for a hackathon.

A realistic first version is:

```text
Terraform
    ↓
API Gateway
    ↓
Lambda
    ↓
Step Functions
    ↓
Neptune
    ↓
Test Environment
    ↓
CloudWatch
    ↓
S3
    ↓
Bedrock
    ↓
Dashboard
```

With:

- IAM for security
- DynamoDB for experiment state

Everything else can be an extension.

---

# The core architecture in one sentence

**Terraform tells ChangeProof what is about to change, Neptune tells it what is connected, Step Functions orchestrates the experiment, IAM controls the boundaries, CloudWatch/X-Ray/CloudTrail provide evidence, S3/DynamoDB store the results, and Bedrock explains those results to the developer.**

---

# The mental model to remember

If you forget everything else, remember this:

```text
Terraform
"What are we changing?"

      ↓

Neptune
"What depends on it?"

      ↓

Prediction
"What do we expect?"

      ↓

Test Environment
"Let's actually try it."

      ↓

CloudWatch
"What happened?"

      ↓

X-Ray
"Where did it happen?"

      ↓

CloudTrail
"What AWS changes occurred?"

      ↓

Comparison
"Prediction vs reality"

      ↓

Bedrock
"Explain the evidence."

      ↓

Decision
"Promote or reject?"
```

That is the heart of **ChangeProof**.
