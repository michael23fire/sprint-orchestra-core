package com.jiraagentic.app;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cache.annotation.EnableCaching;

@SpringBootApplication
@EnableCaching  // 開啟 Cache 功能
public class JiraAgenticAiApplication {
	public static void main(String[] args) {
		SpringApplication.run(JiraAgenticAiApplication.class, args);
	}
}
