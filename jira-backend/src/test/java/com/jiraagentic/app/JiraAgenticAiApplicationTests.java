package com.jiraagentic.app;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.context.ApplicationContext;
import org.springframework.core.env.Environment;
import org.springframework.test.web.servlet.MockMvc;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.containsString;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = {
		// Endpoint availability is under test here; external dependency health is environment-specific.
		"management.health.db.enabled=false",
		"management.health.redis.enabled=false"
})
@AutoConfigureMockMvc
class JiraAgenticAiApplicationTests {

	@Autowired
	private MockMvc mockMvc;
	@Autowired
	private ApplicationContext applicationContext;
	@Autowired
	private Environment environment;

	@Test
	void contextLoads() {
	}

	@Test
	void consolidatedApplicationConfigurationIsLoadedAndDemoControllerIsAbsent() {
		assertThat(environment.getProperty("spring.application.name")).isEqualTo("jira-agentic-ai");
		assertThat(applicationContext.containsBean("secureDemoController")).isFalse();
	}

	@Test
	void healthEndpointsAreAvailableWithoutGatewayHeader() throws Exception {
		mockMvc.perform(get("/actuator/health"))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("UP"));

		mockMvc.perform(get("/actuator/health/liveness"))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("UP"));

		mockMvc.perform(get("/actuator/health/readiness"))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("UP"));
	}

	@Test
	void prometheusEndpointIsAvailableWithoutGatewayHeader() throws Exception {
		mockMvc.perform(get("/actuator/prometheus"))
				.andExpect(status().isOk())
				.andExpect(content().string(containsString("jvm_memory_used_bytes")))
				.andExpect(content().string(containsString("application=\"jira-backend\"")));
	}
}
