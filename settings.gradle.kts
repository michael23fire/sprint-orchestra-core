pluginManagement {
    plugins {
        id("org.springframework.boot") version "3.2.0"
        id("io.spring.dependency-management") version "1.1.7"
    }
}

rootProject.name = "jira-agentic-ai"

include("jira-backend", "gateway", "tmp-kafka-consumer-poc")
