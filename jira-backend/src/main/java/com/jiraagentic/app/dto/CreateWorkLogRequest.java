package com.jiraagentic.app.dto;

import lombok.Data;

import java.time.LocalDate;

@Data
public class CreateWorkLogRequest {
    private Long authorId;
    private Integer spentMinutes;
    private String note;
    private LocalDate logDate;
}
