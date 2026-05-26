package com.jiraagentic.app.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;

import java.net.URI;

@Configuration
public class S3StorageConfig {

    @Bean
    public S3Client s3Client(
            @Value("${app.attachments.s3.endpoint:http://localhost:9000}") String endpoint,
            @Value("${app.attachments.s3.region:us-east-1}") String region,
            @Value("${app.attachments.s3.access-key:minioadmin}") String accessKey,
            @Value("${app.attachments.s3.secret-key:minioadmin}") String secretKey,
            @Value("${app.attachments.s3.path-style-access:true}") boolean pathStyleAccess) {

        return S3Client.builder()
                .endpointOverride(URI.create(endpoint))
                .region(Region.of(region))
                .credentialsProvider(
                        StaticCredentialsProvider.create(AwsBasicCredentials.create(accessKey, secretKey)))
                .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(pathStyleAccess).build())
                .build();
    }
}
