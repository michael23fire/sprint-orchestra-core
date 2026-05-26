package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.UserGroup;
import lombok.Data;
import java.util.List;

@Data
public class GroupDto {
    private Long id;
    private String name;
    private String description;
    private Long ownerId;
    private String ownerName;
    private List<UserDto> members;

    public static GroupDto from(UserGroup g) {
        GroupDto dto = new GroupDto();
        dto.setId(g.getId());
        dto.setName(g.getName());
        dto.setDescription(g.getDescription());
        if (g.getOwner() != null) {
            dto.setOwnerId(g.getOwner().getId());
            dto.setOwnerName(g.getOwner().getName());
        }
        return dto;
    }
}
