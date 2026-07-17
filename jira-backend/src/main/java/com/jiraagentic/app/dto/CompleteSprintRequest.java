package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CompleteSprintRequest {
    /** backlog | future_sprint | new_sprint — required only when incomplete issues exist; ignored if none */
    private String incompleteDestination;
    /** Required when incompleteDestination = future_sprint */
    private Long moveToSprintId;
    /** Optional name when incompleteDestination = new_sprint; default "Sprint N" */
    private String newSprintName;
}
