package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.User;
import lombok.Data;

@Data
public class UserDto {
    private Long id;
    private String username;
    private String name;
    private String email;
    private String avatarColor;
    private boolean passwordLoginEnabled;

    public static UserDto from(User u) {
        UserDto dto = new UserDto();
        dto.setId(u.getId());
        dto.setUsername(u.getUsername());
        dto.setName(u.getName());
        dto.setEmail(u.getEmail());
        dto.setAvatarColor(u.getAvatarColor());
        dto.setPasswordLoginEnabled(u.getPassword() != null);
        return dto;
    }
}
