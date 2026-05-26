# Read Me First

The following was discovered as part of building this project:

* Hyphens are not valid in Java package segments. This codebase uses **`com.jiraagentic.app`** as the Spring Boot application root package (the repo / artifact name may still be `jira-agentic-ai`).

**Where things live:** High-level **target architecture** (merged core, Kafka, vectorization & AI services, observability model) is in **`ARCHITECTURE.md`**. Gradle modules (**`jira-backend`**, **`gateway`**, **`tmp-kafka-consumer-poc`**), Docker Compose, Kafka wiring, attachment/Kafka semantics, and IntelliJ **`.run/`** presets are in **`README.md`**. API and schema details are in **`API_DESIGN_CORE_FINAL_v1.md`** and **`DATABASE_SCHEMA_CORE_FINAL_v1.md`**.

# Getting Started

### Reference Documentation

For further reference, please consider the following sections:

* [Official Gradle documentation](https://docs.gradle.org)
* [Spring Boot Gradle Plugin Reference Guide](https://docs.spring.io/spring-boot/docs/3.2.0/gradle-plugin/reference/html/)
* [Create an OCI image](https://docs.spring.io/spring-boot/docs/3.2.0/gradle-plugin/reference/html/#build-image)

### Additional Links

These additional references should also help you:

* [Gradle Build Scans – insights for your project's build](https://scans.gradle.com#gradle)
