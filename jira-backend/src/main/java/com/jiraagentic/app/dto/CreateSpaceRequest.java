package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CreateSpaceRequest {
    private String name;
    private String key;
    private String color;
}
