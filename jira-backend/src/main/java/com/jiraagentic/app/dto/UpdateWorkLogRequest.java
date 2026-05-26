package com.jiraagentic.app.dto;

import lombok.Data;

import java.time.LocalDate;

@Data
public class UpdateWorkLogRequest {
    private Integer spentMinutes;
    private String note;
    private LocalDate logDate;
}
