// Dump generated on: 2026-08-20 06:48:48 PM UTC+3
// Validated on discord. Build 24840484.
// Full IL2CPP class/field/function dump — TypeDefinitionIndex-anchored, no
// [AUTO]/[PENDING]/[PROBED] uncertainty tags, unlike the NeoRed SDK v6 dump
// (offsets_decrypts_export_24840484.h) captured the same day. Where the two
// conflicted, this file's values were preferred in OFF (legacy_runtime.py) —
// see the inline comments there for which fields that applied to
// (LocalPlayer_c/LocalPlayer_Entity, item_heldEntity, itemdef_itemModWearable,
// be_position_lerp/be_lerp_nested/be_lerp_world_pos, mainCameraTransform).

#include <string>

inline std::string Build = "24840484";

namespace GameAssembly {
    constexpr const static size_t timestamp = 0x6a86ecb0;
    constexpr const static size_t il2cpp_resolve_icall = 0x878e50;
    constexpr const static size_t il2cpp_array_new = 0x878e70;
    constexpr const static size_t il2cpp_assembly_get_image = 0x3c30;
    constexpr const static size_t il2cpp_class_from_name = 0x863060;
    constexpr const static size_t il2cpp_class_get_method_from_name = 0x879270;
    constexpr const static size_t il2cpp_class_get_type = 0x74f170;
    constexpr const static size_t il2cpp_domain_get = 0x879b90;
    constexpr const static size_t il2cpp_domain_get_assemblies = 0x879bb0;
    constexpr const static size_t il2cpp_gchandle_get_target = 0x87a2c0;
    constexpr const static size_t il2cpp_gchandle_new = 0x87a270;
    constexpr const static size_t il2cpp_gchandle_free = 0x87a360;
    constexpr const static size_t il2cpp_method_get_name = 0xd400;
    constexpr const static size_t il2cpp_object_new = 0x87ac00;
    constexpr const static size_t il2cpp_type_get_object = 0x87bd30;
}

#define Object_TypeDefinitionIndex 463

namespace Object_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11624888;

// Offsets
    constexpr const static size_t m_CachedPtr = 0x10;

// Functions
    constexpr const static size_t GetInstanceID = 0xe78c730;
    constexpr const static size_t get_name = 0xe271710;
    constexpr const static size_t Destroy = 0xe78d750;
    constexpr const static size_t DestroyImmediate = 0xe78d880;
    constexpr const static size_t DontDestroyOnLoad = 0xe78da80;
    constexpr const static size_t FindObjectFromInstanceID = 0xe78efa0;
    constexpr const static size_t GetName = 0xc7f20;
    constexpr const static size_t get_hideFlags = 0xe78db70;
    constexpr const static size_t set_hideFlags = 0xe78dc30;
}

#define Resources_TypeDefinitionIndex 376

namespace Resources_Offsets {

// Functions
    constexpr const static size_t FindObjectsOfTypeAll = 0xe778f20;
}

namespace Object {
    inline constexpr std::uintptr_t m_CachedPtr = 0x10;
}

namespace unity_string {
    inline constexpr std::uintptr_t m_stringLength = 0x10;
    inline constexpr std::uintptr_t first_char = 0x14;
}

namespace system_list {
    inline constexpr std::uintptr_t array = 0x10;
    inline constexpr std::uintptr_t size = 0x18;
    inline constexpr std::uintptr_t array_first_element = 0x20;
}

namespace entitylist {
    inline constexpr std::uintptr_t array = 0x10;
    inline constexpr std::uintptr_t size = 0x18;
    inline constexpr std::uintptr_t first_elem = 0x20;
}

namespace unity_component {
    inline constexpr std::uintptr_t game_object = 0x20;
}

namespace unity_game_object {
    inline constexpr std::uintptr_t components = 0x20;
    inline constexpr std::uintptr_t component_count = 0x30;
    inline constexpr std::uintptr_t component_stride = 0x10;
    inline constexpr std::uintptr_t component_ptr_in_entry = 0x8;
}

#define GameObject_TypeDefinitionIndex 430

namespace GameObject_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1162c9f0;

// Functions
    constexpr const static size_t SetActive = 0xe784c00;
    constexpr const static size_t Internal_AddComponentWithType = 0xe784670;
    constexpr const static size_t GetComponent = 0xe783be0;
    constexpr const static size_t GetComponentCount = 0xe784740;
    constexpr const static size_t GetComponentCount_Injected = 0x78f40;
    constexpr const static size_t GetComponentInChildren = 0xe783d70;
    constexpr const static size_t GetComponentInParent = 0xe783e60;
    constexpr const static size_t GetComponentsInternal = 0xe783f40;
    constexpr const static size_t Internal_CreateGameObject = 0xe785ef0;
    constexpr const static size_t get_layer = 0xe784960;
    constexpr const static size_t get_tag = 0xe785090;
    constexpr const static size_t get_transform = 0xe7847e0;
}

#define Component_TypeDefinitionIndex 416

namespace Component_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1156d4c0;

// Functions
    constexpr const static size_t get_gameObject = 0xe77f950;
    constexpr const static size_t get_gameObject_Injected = 0xbe8e0;
    constexpr const static size_t get_transform = 0xe77f890;
}

#define Behaviour_TypeDefinitionIndex 411

namespace Behaviour_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11588d80;

// Functions
    constexpr const static size_t get_enabled = 0xb59970;
    constexpr const static size_t set_enabled = 0xe77ec70;
}

#define Transform_TypeDefinitionIndex 505

namespace Transform_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115ae8f8;

// Functions
    constexpr const static size_t get_eulerAngles = 0xe79d3a0;
    constexpr const static size_t GetChild = 0xe7a2110;
    constexpr const static size_t GetParent = 0xe79e9d0;
    constexpr const static size_t GetRoot = 0xe7a1330;
    constexpr const static size_t InverseTransformDirection_Injected = 0xd0450;
    constexpr const static size_t InverseTransformPoint_Injected = 0xd07b0;
    constexpr const static size_t InverseTransformVector_Injected = 0xd0610;
    constexpr const static size_t GetPositionAndRotation = 0xe79ee80;
    constexpr const static size_t SetLocalPositionAndRotation_Injected = 0xd0280;
    constexpr const static size_t SetPositionAndRotation_Injected = 0xd0240;
    constexpr const static size_t TransformDirection_Injected = 0xd0380;
    constexpr const static size_t TransformPoint_Injected = 0xd06e0;
    constexpr const static size_t TransformVector_Injected = 0xd0540;
    constexpr const static size_t get_childCount = 0xe7a13f0;
    constexpr const static size_t get_forward_Injected = 0xe79e000;
    constexpr const static size_t get_right_Injected = 0xe79d7e0;
    constexpr const static size_t get_up_Injected = 0xe79dbf0;
    constexpr const static size_t get_localPosition_Injected = 0xcfd30;
    constexpr const static size_t get_localRotation_Injected = 0xcfe70;
    constexpr const static size_t get_localScale_Injected = 0xcff50;
    constexpr const static size_t get_lossyScale_Injected = 0xd0e30;
    constexpr const static size_t get_position_Injected = 0xcfcc0;
    constexpr const static size_t get_rotation_Injected = 0xcfde0;
    constexpr const static size_t set_localPosition_Injected = 0xcfd60;
    constexpr const static size_t set_localRotation_Injected = 0xcff10;
    constexpr const static size_t set_localScale_Injected = 0xcff80;
    constexpr const static size_t set_position_Injected = 0xcfcf0;
    constexpr const static size_t set_rotation_Injected = 0xcfe30;
}

#define Camera_TypeDefinitionIndex 167

namespace Camera_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115ba940;

// Offsets
    inline constexpr std::uintptr_t m_Aspect = 0x4E0;
    inline constexpr std::uintptr_t m_CustomProjectionMatrix = 0xB0;
    inline constexpr std::uintptr_t m_UseCustomProjectionMatrix = 0x504;

// Functions
    constexpr const static size_t get_main = 0xe6fe280;
    constexpr const static size_t WorldToScreenPoint_Injected = 0x7a230;
    constexpr const static size_t ScreenToWorldPoint_Injected = 0x7a4e0;
    constexpr const static size_t GetAllCamerasCount = 0xe6fedd0;
    constexpr const static size_t CopyFrom = 0xe6ff570;
    constexpr const static size_t get_fieldOfView = 0xe6f8550;
    constexpr const static size_t set_fieldOfView = 0xe6f85f0;
    constexpr const static size_t get_nearClipPlane = 0xe6f82b0;
    constexpr const static size_t set_nearClipPlane = 0xe6f8350;
    constexpr const static size_t get_farClipPlane = 0xe6f8400;
    constexpr const static size_t set_farClipPlane = 0xe6f84a0;
    constexpr const static size_t get_depth = 0xe6f91b0;
    constexpr const static size_t set_depth = 0xe6f9250;
    constexpr const static size_t get_projectionMatrix_Injected = 0x7a080;
    constexpr const static size_t set_projectionMatrix_Injected = 0x7a0c0;
    constexpr const static size_t set_aspect = 0xe6f93a0;
    constexpr const static size_t set_aspect_Injected = 0x78620;
    constexpr const static size_t set_cullingMask = 0xe6f95b0;
    constexpr const static size_t set_clearFlags = 0xe6fa670;
    constexpr const static size_t set_backgroundColor_Injected = 0x78a30;
    constexpr const static size_t set_targetTexture = 0xe6fc4c0;
    constexpr const static size_t Render = 0xe6ff150;
    constexpr const static size_t RenderWithShader = 0xe6ff1f0;
}

#define CapsuleCollider_TypeDefinitionIndex 6

namespace CapsuleCollider_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x116428a8;

// Functions
    constexpr const static size_t set_radius = 0xe872e50;
}

#define Time_TypeDefinitionIndex 489

namespace Time_Offsets {

// Functions
    constexpr const static size_t get_deltaTime = 0x7eae850;
    constexpr const static size_t get_fixedDeltaTime = 0xe7976c0;
    constexpr const static size_t get_fixedTime = 0x7eaee60;
    constexpr const static size_t get_frameCount = 0xe797930;
    constexpr const static size_t get_realtimeSinceStartup = 0x7ead880;
    constexpr const static size_t get_smoothDeltaTime = 0xe7977d0;
    constexpr const static size_t get_time = 0x7fcf1b0;
}

#define Material_TypeDefinitionIndex 241

namespace Material_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11629208;

// Functions
    constexpr const static size_t SetFloatImpl = 0xe7343a0;
    constexpr const static size_t SetColorImpl_Injected = 0x9e180;
    constexpr const static size_t SetTextureImpl = 0xe734640;
    constexpr const static size_t CreateWithMaterial = 0xe730b90;
    constexpr const static size_t CreateWithShader = 0xe730a90;
    constexpr const static size_t SetBufferImpl = 0xe734760;
    constexpr const static size_t set_shader = 0xe731060;
    constexpr const static size_t get_shader = 0xe730f80;
}

#define MaterialPropertyBlock_TypeDefinitionIndex 236

namespace MaterialPropertyBlock_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115ae530;

// Functions
    constexpr const static size_t ctor = 0xe726660;
    constexpr const static size_t SetFloatImpl = 0xe724cd0;
    constexpr const static size_t SetTextureImpl = 0xe725920;
}

#define Shader_TypeDefinitionIndex 240

namespace Shader_Offsets {

// Functions
    constexpr const static size_t Find = 0xe72d570;
    constexpr const static size_t PropertyToID = 0xe72e3b0;
    constexpr const static size_t GetPropertyCount = 0xe72fc80;
    constexpr const static size_t GetPropertyName = 0xe72f870;
    constexpr const static size_t GetPropertyType = 0xe72f9d0;
}

#define Mesh_TypeDefinitionIndex 300

namespace Mesh_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1162cc88;

// Functions
    constexpr const static size_t Internal_Create = 0xe7408c0;
    constexpr const static size_t MarkDynamicImpl = 0xe7441e0;
    constexpr const static size_t ClearImpl = 0xe743f20;
    constexpr const static size_t set_subMeshCount = 0xe743a20;
    constexpr const static size_t SetVertexBufferParamsFromPtr = 0xa4980;
    constexpr const static size_t InternalSetVertexBufferData = 0xa4a30;
    constexpr const static size_t UploadMeshDataImpl = 0xe744320;
}

#define Renderer_TypeDefinitionIndex 238

namespace Renderer_Offsets {

// Functions
    constexpr const static size_t get_enabled = 0xe728d20;
    constexpr const static size_t get_isVisible = 0xe728e70;
    constexpr const static size_t GetMaterial = 0xe728280;
    constexpr const static size_t GetMaterialArray = 0xe7284e0;
}

#define Texture_TypeDefinitionIndex 305

namespace Texture_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11590b40;

// Functions
    constexpr const static size_t set_filterMode = 0xe751830;
    constexpr const static size_t GetNativeTexturePtr = 0xe751cf0;
}

#define Texture2D_TypeDefinitionIndex 306

namespace Texture2D_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11628f80;

// Functions
    constexpr const static size_t ctor = 0xe756130;
    constexpr const static size_t Internal_CreateImpl = 0xac280;
    constexpr const static size_t GetWritableImageData = 0xe754ef0;
    constexpr const static size_t ApplyImpl = 0xe754460;
}

#define Sprite_TypeDefinitionIndex 143

namespace Sprite_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1153cda8;

// Functions
    constexpr const static size_t get_texture = 0xe6f0110;
}

#define RenderTexture_TypeDefinitionIndex 312

namespace RenderTexture_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1165b0e8;

// Functions
    constexpr const static size_t GetTemporary = 0xe7623a0;
    constexpr const static size_t ReleaseTemporary = 0xe75fd40;
}

#define CommandBuffer_TypeDefinitionIndex 895

namespace CommandBuffer_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115bae68;

// Functions
    constexpr const static size_t ctor = 0xe7cfc90;
    constexpr const static size_t Clear = 0xe7c3e80;
    constexpr const static size_t SetRenderTargetSingle_Internal_Injected = 0xefba0;
    constexpr const static size_t ClearRenderTarget_Injected = 0xe7c7b20;
    constexpr const static size_t SetViewport_Injected = 0xe94c0;
    constexpr const static size_t SetViewProjectionMatrices_Injected = 0xee790;
    constexpr const static size_t EnableScissorRect_Injected = 0xe9580;
    constexpr const static size_t DisableScissorRect = 0xe7c55c0;
    constexpr const static size_t Internal_DrawProceduralIndexedIndirect_Injected = 0xe8fe0;
    constexpr const static size_t Internal_DrawMesh_Injected = 0xe8010;
    constexpr const static size_t Internal_DrawRenderer = 0xe7c4280;
}

#define RenderTargetIdentifier_TypeDefinitionIndex 855

namespace RenderTargetIdentifier_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115b0cf0;

// Functions
    constexpr const static size_t ctor = 0xe7bad50;
}

#define ComputeBuffer_TypeDefinitionIndex 479

namespace ComputeBuffer_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x116288a0;

// Functions
    constexpr const static size_t ctor = 0xe791330;
    constexpr const static size_t get_count = 0xe791650;
    constexpr const static size_t Release = 0xe791570;
    constexpr const static size_t InternalSetNativeData = 0xe791c20;
}

#define GraphicsBuffer_TypeDefinitionIndex 243

namespace GraphicsBuffer_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115bb120;

// Functions
    constexpr const static size_t ctor = 0xe738510;
    constexpr const static size_t get_count = 0xe738a80;
    constexpr const static size_t Dispose = 0xe738210;
    constexpr const static size_t InternalSetNativeData = 0xe739050;
}

#define Event_TypeDefinitionIndex 1

namespace Event_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1160c888;

// Functions
    constexpr const static size_t get_current = 0xe808120;
    constexpr const static size_t get_type = 0xe807190;
    constexpr const static size_t PopEvent = 0xe8076e0;
    constexpr const static size_t Internal_Use = 0xe8074d0;
}

#define Graphics_TypeDefinitionIndex 216

namespace Graphics_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11629210;

// Functions
    constexpr const static size_t Internal_BlitMaterial5 = 0xe717550;
    constexpr const static size_t ExecuteCommandBuffer = 0xe717af0;
}

#define Matrix4x4_TypeDefinitionIndex 338

namespace Matrix4x4_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11591038;

// Functions
    constexpr const static size_t Ortho_Injected = 0xb5e80;
}

#define AssetBundle_TypeDefinitionIndex 1

namespace AssetBundle_Offsets {

// Functions
    constexpr const static size_t LoadFromFile_Internal = 0xe6d81a0;
    constexpr const static size_t LoadAsset_Internal = 0xe6d8640;
    constexpr const static size_t Unload = 0xe6d8db0;
}

#define Screen_TypeDefinitionIndex 213

namespace Screen_Offsets {

// Functions
    constexpr const static size_t get_width = 0x800ebe0;
    constexpr const static size_t get_height = 0x800e930;
}

#define Input_TypeDefinitionIndex 9

namespace Input_Offsets {

// Functions
    constexpr const static size_t get_mousePosition_Injected = 0x1ac850;
    constexpr const static size_t get_mouseScrollDelta_Injected = 0x1aca20;
    constexpr const static size_t GetMouseButtonDown = 0xe84c140;
    constexpr const static size_t GetMouseButtonUp = 0xe84c190;
    constexpr const static size_t GetMouseButton = 0xe84c0f0;
    constexpr const static size_t GetKeyDownInt = 0xe84c0a0;
    constexpr const static size_t GetKeyInt = 0xe84c050;
}

#define Application_TypeDefinitionIndex 150

namespace Application_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x116247a0;

// Functions
    constexpr const static size_t get_version = 0xe6f4e60;
    constexpr const static size_t Quit = 0xe6f3d80;
    constexpr const static size_t get_isFocused = 0xe6f4140;
}

#define Gradient_TypeDefinitionIndex 335

namespace Gradient_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1155e620;

// Functions
    constexpr const static size_t SetKeys = 0xb5ac0;
}

#define Physics_TypeDefinitionIndex 14

namespace Physics_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115a5ea8;

// Functions
    constexpr const static size_t Raycast = 0xe876310;
    constexpr const static size_t RaycastNonAlloc = 0xe878970;
    constexpr const static size_t CheckCapsule = 0xe87a0b0;
}

#define Image_TypeDefinitionIndex 39

namespace Image_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115b8018;

// Offsets
    constexpr const static size_t m_Sprite = 0xe0;
}

#define GraphicsSettings_TypeDefinitionIndex 885

namespace GraphicsSettings_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11624898;

// Functions
    constexpr const static size_t get_INTERNAL_defaultRenderPipeline = 0xe7bbec0;
}

#define Cursor_TypeDefinitionIndex 323

namespace Cursor_Offsets {

// Functions
    constexpr const static size_t get_visible = 0xe765fe0;
}

// obf name: ::%c2ffa6d6c30a51fdcebf2d1dc9d81a8a0a791dda
#define PlayerProjectileUpdate_ClassName "%c2ffa6d6c30a51fdcebf2d1dc9d81a8a0a791dda"
#define PlayerProjectileUpdate_ClassNameShort "%c2ffa6d6c30a51fdcebf2d1dc9d81a8a0a791dda"
#define PlayerProjectileUpdate_TypeDefinitionIndex 322

namespace PlayerProjectileUpdate_Offsets {

// Offsets
    constexpr const static size_t curPosition = 0x1c;
    constexpr const static size_t travelTime = 0x14;
    constexpr const static size_t projectileID = 0x10;
    constexpr const static size_t curVelocity = 0x2c;
}

// obf name: ::%0c8d025e6ca2cf9031759c9ec09caee85e6ce858
#define PlayerProjectileAttack_ClassName "%0c8d025e6ca2cf9031759c9ec09caee85e6ce858"
#define PlayerProjectileAttack_ClassNameShort "%0c8d025e6ca2cf9031759c9ec09caee85e6ce858"
#define PlayerProjectileAttack_TypeDefinitionIndex 65

namespace PlayerProjectileAttack_Offsets {

// Offsets
    constexpr const static size_t playerAttack = 0x30;
    constexpr const static size_t travelTime = 0x10;
    constexpr const static size_t hitVelocity = 0x14;
    constexpr const static size_t hitDistance = 0x24;
}

// obf name: ::%65985bac30b1e858bfd413e8857c3dee22f8ff78
#define PlayerAttack_ClassName "%65985bac30b1e858bfd413e8857c3dee22f8ff78"
#define PlayerAttack_ClassNameShort "%65985bac30b1e858bfd413e8857c3dee22f8ff78"
#define PlayerAttack_TypeDefinitionIndex 755

namespace PlayerAttack_Offsets {

// Offsets
    constexpr const static size_t attack = 0x18;
    constexpr const static size_t projectileID = 0x10;
}

// obf name: ::%10ef728f13cc8e2d442cf284b5bac8d0517016e8
#define ProtoBuf_Attack_ClassName "%10ef728f13cc8e2d442cf284b5bac8d0517016e8"
#define ProtoBuf_Attack_ClassNameShort "%10ef728f13cc8e2d442cf284b5bac8d0517016e8"
#define ProtoBuf_Attack_TypeDefinitionIndex 602

namespace ProtoBuf_Attack_Offsets {

// Offsets
    constexpr const static size_t pointStart = 0x10;
    constexpr const static size_t hitPosLocal = 0x1c;
    constexpr const static size_t hitPosWorld = 0x2c;
    constexpr const static size_t hitNrmWorld = 0x38;
    constexpr const static size_t hitNrmLocal = 0x64;
    constexpr const static size_t pointEnd = 0x70;
    constexpr const static size_t hitID = 0x58;
}

#define BaseNetworkable_TypeDefinitionIndex 8375

namespace BaseNetworkable_Offsets {

// Offsets
    constexpr const static size_t prefabID = 0x54;
    constexpr const static size_t globalBroadcast = 0x58;
    constexpr const static size_t networkRange = 0x64;
    constexpr const static size_t parentEntity = 0x38;
    constexpr const static size_t children = 0x88;
    constexpr const static size_t net = 0x70;
}

// obf name: ::%7c64cfea06747d5d613cdd9ba3a1aa27e3e84986
#define BaseNetworkable_Static_ClassName "BaseNetworkable/%7c64cfea06747d5d613cdd9ba3a1aa27e3e84986"
#define BaseNetworkable_Static_ClassNameShort "%7c64cfea06747d5d613cdd9ba3a1aa27e3e84986"
#define BaseNetworkable_Static_TypeDefinitionIndex 8382

namespace BaseNetworkable_Static_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115b1a70;

// Offsets
    constexpr const static size_t clientEntities = 0x8;
}

// obf name: ::%a533004bfb905ca7858c4ee22baabd23d9408224
#define BaseNetworkable_EntityRealm_ClassName "BaseNetworkable/%a533004bfb905ca7858c4ee22baabd23d9408224"
#define BaseNetworkable_EntityRealm_ClassNameShort "%a533004bfb905ca7858c4ee22baabd23d9408224"
#define BaseNetworkable_EntityRealm_TypeDefinitionIndex 8380

namespace BaseNetworkable_EntityRealm_Offsets {

// Offsets
    constexpr const static size_t entityList = 0x10;

// Functions
    constexpr const static size_t Find = 0x66394a0;
}

// obf name: ::%f38be2093b8fa1435c1a1b0986a347d706badd5c
#define System_ListDictionary_ClassName "%f38be2093b8fa1435c1a1b0986a347d706badd5c<%127acf0b94c35478df5fd3670d74e12fdc057f1d,BaseNetworkable>"
#define System_ListDictionary_ClassNameShort "%f38be2093b8fa1435c1a1b0986a347d706badd5c"
#define System_ListDictionary_TypeDefinitionIndex 69

namespace System_ListDictionary_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11589438;

// Offsets
    constexpr const static size_t vals = 0x18;

// Functions
    constexpr const static size_t TryGetValue = 0x9e932c0;
    constexpr const static size_t TryGetValue_methodinfo = 0x11589478;
}

// obf name: ::%c6142ee6b7b589ebea7d8c1939e4ff1957aba837
#define System_BufferList_ClassName "%c6142ee6b7b589ebea7d8c1939e4ff1957aba837<BaseNetworkable>"
#define System_BufferList_ClassNameShort "%c6142ee6b7b589ebea7d8c1939e4ff1957aba837"
#define System_BufferList_TypeDefinitionIndex 102

namespace System_BufferList_Offsets {

// Offsets
    constexpr const static size_t count = 0x18;
    constexpr const static size_t buffer = 0x10;
}

// obf name: ::SingletonComponent`1
#define SingletonComponent_ClassName "SingletonComponent<MainCamera>"
#define SingletonComponent_ClassNameShort "SingletonComponent`1"
#define SingletonComponent_TypeDefinitionIndex 31

namespace SingletonComponent_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115435b8;

// Offsets
    constexpr const static size_t Instance = 0x8;
}

#define Model_TypeDefinitionIndex 4100

namespace Model_Offsets {

// Offsets
    constexpr const static size_t rootBone = 0x28;
    constexpr const static size_t headBone = 0x30;
    constexpr const static size_t eyeBone = 0x38;
    constexpr const static size_t boneTransforms = 0x50;
    constexpr const static size_t boneNames = 0x58;
}

#define BaseEntity_TypeDefinitionIndex 863

namespace BaseEntity_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115dfd30;

// Offsets
    constexpr const static size_t bounds = 0x18c;
    constexpr const static size_t model = 0x1b8;
    constexpr const static size_t flags = 0x1c0;
    constexpr const static size_t triggers = 0x138;
    constexpr const static size_t positionLerp = 0xc8;

// Functions
    constexpr const static size_t ServerRPC = 0x604ee20;
    constexpr const static size_t FindBone = 0x5fdcec0;
    constexpr const static size_t GetWorldVelocity = 0x5fb7780;
    constexpr const static size_t GetParentVelocity = 0x604e3f0;
}

// obf name: ::%bf51738c5558aec0f6808676859319476cb115d0
#define PositionLerp_ClassName "%bf51738c5558aec0f6808676859319476cb115d0"
#define PositionLerp_ClassNameShort "%bf51738c5558aec0f6808676859319476cb115d0"
#define PositionLerp_TypeDefinitionIndex 2018

namespace PositionLerp_Offsets {

// Offsets
    constexpr const static size_t interpolator = 0x10;
}

// obf name: ::%9d4c53104e0a29a92e026102a5fa9fdc80d6749f
#define Interpolator_ClassName "%9d4c53104e0a29a92e026102a5fa9fdc80d6749f<%abf8e6151a149473127f18aece0f0a884bb466d2>"
#define Interpolator_ClassNameShort "%9d4c53104e0a29a92e026102a5fa9fdc80d6749f"
#define Interpolator_TypeDefinitionIndex 5168

namespace Interpolator_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11611a70;

// Offsets
    constexpr const static size_t list = 0x30;
    constexpr const static size_t last = 0x10;
}

#define BaseCombatEntity_TypeDefinitionIndex 4688

namespace BaseCombatEntity_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1159e908;

// Offsets
    constexpr const static size_t skeletonProperties = 0x230;
    constexpr const static size_t baseProtection = 0x238;
    constexpr const static size_t startHealth = 0x240;
    constexpr const static size_t lifestate = 0x2a8;
    constexpr const static size_t markAttackerHostile = 0x2ae;
    constexpr const static size_t _health = 0x2b4;
    constexpr const static size_t _maxHealth = 0x2b8;
    constexpr const static size_t lastNotifyFrame = 0x2c8;
}

#define SkeletonProperties_TypeDefinitionIndex 769

namespace SkeletonProperties_Offsets {

// Offsets
    constexpr const static size_t bones = 0x20;
    constexpr const static size_t quickLookup = 0x28;
}

#define SkeletonProperties_BoneProperty_TypeDefinitionIndex 770

namespace SkeletonProperties_BoneProperty_Offsets {

// Offsets
    constexpr const static size_t boneName = 0x18;
    constexpr const static size_t area = 0x20;
}

#define DamageProperties_TypeDefinitionIndex 608

namespace DamageProperties_Offsets {

// Offsets
    constexpr const static size_t bones = 0x20;
}

#define DamageProperties_HitAreaProperty_TypeDefinitionIndex 609

namespace DamageProperties_HitAreaProperty_Offsets {

// Offsets
    constexpr const static size_t area = 0x10;
    constexpr const static size_t damage = 0x14;
}

// obf name: ::%46cbd776322e0e90599c7866250e82e9b6730e90
#define DamageTypeList_ClassName "%46cbd776322e0e90599c7866250e82e9b6730e90"
#define DamageTypeList_ClassNameShort "%46cbd776322e0e90599c7866250e82e9b6730e90"
#define DamageTypeList_TypeDefinitionIndex 5414

namespace DamageTypeList_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11542a58;

// Offsets
    constexpr const static size_t types = 0x10;
}

#define ProtectionProperties_TypeDefinitionIndex 8566

namespace ProtectionProperties_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115c4928;

// Offsets
    constexpr const static size_t amounts = 0x30;
}

#define ItemDefinition_TypeDefinitionIndex 8218

namespace ItemDefinition_Offsets {

// Offsets
    constexpr const static size_t itemid = 0x20;
    constexpr const static size_t shortname = 0x28;
    constexpr const static size_t displayName = 0x40;
    constexpr const static size_t iconSprite = 0x50;
    constexpr const static size_t category = 0x58;
    constexpr const static size_t stackable = 0x78;
    constexpr const static size_t rarity = 0x94;
    constexpr const static size_t condition = 0xb8;
    constexpr const static size_t ItemModWearable = 0x190;
}

#define RecoilProperties_TypeDefinitionIndex 7203

namespace RecoilProperties_Offsets {

// Offsets
    constexpr const static size_t recoilYawMin = 0x18;
    constexpr const static size_t recoilYawMax = 0x1c;
    constexpr const static size_t recoilPitchMin = 0x20;
    constexpr const static size_t recoilPitchMax = 0x24;
    constexpr const static size_t timeToTakeMin = 0x28;
    constexpr const static size_t timeToTakeMax = 0x2c;
    constexpr const static size_t ADSScale = 0x30;
    constexpr const static size_t movementPenalty = 0x34;
    constexpr const static size_t clampPitch = 0x38;
    constexpr const static size_t pitchCurve = 0x40;
    constexpr const static size_t yawCurve = 0x48;
    constexpr const static size_t overrideAimconeWithCurve = 0x5c;
    constexpr const static size_t aimconeCurveScale = 0x60;
    constexpr const static size_t aimconeProbabilityCurve = 0x70;
    constexpr const static size_t ammoAimconeScaleMultiProjectile = 0x78;
    constexpr const static size_t ammoAimconeScaleSingleProjectile = 0x7c;
    constexpr const static size_t newRecoilOverride = 0x80;
}

#define BaseProjectile_Magazine_Definition_TypeDefinitionIndex 4737

namespace BaseProjectile_Magazine_Definition_Offsets {

// Offsets
    constexpr const static size_t builtInSize = 0x0;
}

#define BaseProjectile_Magazine_TypeDefinitionIndex 4736

namespace BaseProjectile_Magazine_Offsets {

// Offsets
    constexpr const static size_t definition = 0x10;
    constexpr const static size_t capacity = 0x18;
    constexpr const static size_t contents = 0x1c;
    constexpr const static size_t ammoType = 0x20;
}

#define AttackEntity_TypeDefinitionIndex 8980

namespace AttackEntity_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11534828;

// Offsets
    constexpr const static size_t deployDelay = 0x2e8;
    constexpr const static size_t repeatDelay = 0x2ec;
    constexpr const static size_t animationDelay = 0x2f0;
    constexpr const static size_t effectiveRange = 0x2f4;
    constexpr const static size_t attackLengthMin = 0x2fc;
    constexpr const static size_t attackLengthMax = 0x300;
    constexpr const static size_t attackSpacing = 0x304;
    constexpr const static size_t noHeadshots = 0x33e;
    constexpr const static size_t nextAttackTime = 0x340;
    constexpr const static size_t timeSinceDeploy = 0x358;

// Functions
    constexpr const static size_t StartAttackCooldown = 0x6d0e2c0;
}

#define BaseProjectile_TypeDefinitionIndex 4735

namespace BaseProjectile_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115e53b0;

// Offsets
    constexpr const static size_t projectileVelocityScale = 0x38c;
    constexpr const static size_t MuzzlePoint = 0x3c8;
    constexpr const static size_t automatic = 0x390;
    constexpr const static size_t reloadTime = 0x3d0;
    constexpr const static size_t primaryMagazine = 0x3d8;
    constexpr const static size_t fractionalReload = 0x3e0;
    constexpr const static size_t aimSway = 0x3f8;
    constexpr const static size_t aimSwaySpeed = 0x3fc;
    constexpr const static size_t recoil = 0x400;
    constexpr const static size_t aimconeCurve = 0x408;
    constexpr const static size_t aimCone = 0x410;
    constexpr const static size_t hipAimCone = 0x414;
    constexpr const static size_t aimconePenaltyPerShot = 0x418;
    constexpr const static size_t aimConePenaltyMax = 0x41c;
    constexpr const static size_t aimconePenaltyRecoverTime = 0x420;
    constexpr const static size_t aimconePenaltyRecoverDelay = 0x424;
    constexpr const static size_t stancePenaltyScale = 0x428;
    constexpr const static size_t noAimingWhileCycling = 0x42d;
    constexpr const static size_t hasADS = 0x42c;
    constexpr const static size_t manualCycle = 0x42e;
    constexpr const static size_t isBurstWeapon = 0x437;
    constexpr const static size_t needsCycle = 0x434;
    constexpr const static size_t canChangeFireModes = 0x438;
    constexpr const static size_t internalBurstFireRateScale = 0x440;
    constexpr const static size_t internalBurstAimConeScale = 0x444;
    constexpr const static size_t cachedModHash = 0x468;
    constexpr const static size_t stancePenalty = 0x450;
    constexpr const static size_t aimconePenalty = 0x458;
    constexpr const static size_t sightAimConeScale = 0x46c;
    constexpr const static size_t sightAimConeOffset = 0x470;
    constexpr const static size_t hipAimConeScale = 0x474;
    constexpr const static size_t hipAimConeOffset = 0x478;
    constexpr const static size_t isReloading = 0x47c;

// Functions
    constexpr const static size_t LaunchProjectileClientSide = 0x3a004c0;
    constexpr const static size_t ScaleRepeatDelay = 0x39ff6a0;
}

#define BaseLauncher_TypeDefinitionIndex 1577

namespace BaseLauncher_Offsets {

// Offsets
    constexpr const static size_t initialSpeedMultiplier = 0x4b8;
}

#define SpinUpWeapon_TypeDefinitionIndex 1834

namespace SpinUpWeapon_Offsets {

// Offsets
}

// obf name: ::%a0f22607afe2280b734af313567745b5857c224e
#define HitTest_ClassName "%a0f22607afe2280b734af313567745b5857c224e"
#define HitTest_ClassNameShort "%a0f22607afe2280b734af313567745b5857c224e"
#define HitTest_TypeDefinitionIndex 7122

namespace HitTest_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115ddca8;

// Offsets
    constexpr const static size_t type = 0xd4;
    constexpr const static size_t AttackRay = 0x9c;
    constexpr const static size_t rayOrigin = 0x9c;
    constexpr const static size_t rayDirection = 0xa8;
    constexpr const static size_t RayHit = 0x70;
    constexpr const static size_t damageProperties = 0x60;
    constexpr const static size_t gameObject = 0x10;
    constexpr const static size_t collider = 0x20;
    constexpr const static size_t ignoredTypes = 0xc8;
    constexpr const static size_t MaxDistance = 0x28;
    constexpr const static size_t Forgiveness = 0x40;
    constexpr const static size_t HitDistance = 0x54;
    constexpr const static size_t Radius = 0x58;
    constexpr const static size_t HitNormal = 0x44;
    constexpr const static size_t HitPoint = 0xb4;
    constexpr const static size_t HitEntity = 0x38;
    constexpr const static size_t ignoreEntity = 0x68;
    constexpr const static size_t HitTransform = 0x18;
    constexpr const static size_t HitPart = 0xd0;
    constexpr const static size_t HitMaterial = 0x30;
}

#define Projectile_TypeDefinitionIndex 7713

namespace Projectile_Offsets {

// Offsets
    constexpr const static size_t initialVelocity = 0x28;
    constexpr const static size_t drag = 0x34;
    constexpr const static size_t gravityModifier = 0x38;
    constexpr const static size_t thickness = 0x3c;
    constexpr const static size_t initialDistance = 0x44;
    constexpr const static size_t swimScale = 0xf0;
    constexpr const static size_t swimSpeed = 0xfc;
    constexpr const static size_t damageProperties = 0x78;
    constexpr const static size_t weaponA = 0x130;
    constexpr const static size_t weaponB = 0x138;
    constexpr const static size_t sourceProjectilePrefab = 0x118;
    constexpr const static size_t projectileID = 0x140;
    constexpr const static size_t owner = 0x120;
    constexpr const static size_t mod = 0x1e8;
    constexpr const static size_t modProjectile = 0x1e8;
    constexpr const static size_t hitTest = 0x1d8;
    constexpr const static size_t currentVelocity = 0x15c;
    constexpr const static size_t currentPosition = 0x168;
    constexpr const static size_t sentPosition = 0x180;
    constexpr const static size_t previousPosition = 0x18c;
    constexpr const static size_t previousVelocity = 0x198;
    constexpr const static size_t sentVelocity = 0x198;
    constexpr const static size_t traveledDistance = 0x174;
    constexpr const static size_t traveledTime = 0x178;
    constexpr const static size_t launchTime = 0x17c;
    constexpr const static size_t sendTravelTime = 0x1a4;

// Functions
    constexpr const static size_t CalculateEffectScale = 0x5ed6080;
    constexpr const static size_t CalculateEffectScale_vtableoff = 0x1e8;
    constexpr const static size_t UpdateVelocity = 0x5f0c570;
    constexpr const static size_t Retire = 0x5ed3130;
    constexpr const static size_t DoHit = 0x5eee440;
}

// obf name: ::%8cbdc0789e5a63cb43f5e26a0ac4ba326fca4a97
#define HitInfo_ClassName "%8cbdc0789e5a63cb43f5e26a0ac4ba326fca4a97"
#define HitInfo_ClassNameShort "%8cbdc0789e5a63cb43f5e26a0ac4ba326fca4a97"
#define HitInfo_TypeDefinitionIndex 3666

namespace HitInfo_Offsets {

// Offsets
    constexpr const static size_t damageProperties = 0x98;
    constexpr const static size_t damageTypes = 0x88;

// Functions
    constexpr const static size_t get_boneArea = 0x2e3a370;
}

// obf name: ::%5042b0867a037872bdf2c10459b21dc1e84daa24
#define GameTrace_ClassName "%5042b0867a037872bdf2c10459b21dc1e84daa24"
#define GameTrace_ClassNameShort "%5042b0867a037872bdf2c10459b21dc1e84daa24"
#define GameTrace_TypeDefinitionIndex 4855

namespace GameTrace_Offsets {

// Functions
    constexpr const static size_t Trace = 0x3b81db0;
}

#define BaseMelee_TypeDefinitionIndex 4552

namespace BaseMelee_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115e53a8;

// Offsets
    constexpr const static size_t damageProperties = 0x388;
    constexpr const static size_t maxDistance = 0x3a0;
    constexpr const static size_t attackRadius = 0x3a4;
    constexpr const static size_t isAutomatic = 0x3a8;
    constexpr const static size_t blockSprintOnAttack = 0x3a9;
    constexpr const static size_t gathering = 0x3e0;
    constexpr const static size_t canThrowAsProjectile = 0x380;

// Functions
}

#define FlintStrikeWeapon_TypeDefinitionIndex 7966

namespace FlintStrikeWeapon_Offsets {

// Offsets
    constexpr const static size_t successFraction = 0x4b8;
    constexpr const static size_t strikeRecoil = 0x4c0;
    constexpr const static size_t _didSparkThisFrame = 0x4c8;
}

#define CompoundBowWeapon_TypeDefinitionIndex 1697

namespace CompoundBowWeapon_Offsets {

// Offsets
    constexpr const static size_t stringHoldDurationMax = 0x4d0;
    constexpr const static size_t stringBonusDamage = 0x4d4;
    constexpr const static size_t stringBonusDistance = 0x4d8;
    constexpr const static size_t stringBonusVelocity = 0x4dc;

// Functions
}

// obf name: ::%6909823b72744e30e914d841e9cb44d3db68d431
#define ItemContainer_ClassName "%6909823b72744e30e914d841e9cb44d3db68d431"
#define ItemContainer_ClassNameShort "%6909823b72744e30e914d841e9cb44d3db68d431"
#define ItemContainer_TypeDefinitionIndex 4208

namespace ItemContainer_Offsets {

// Offsets
    constexpr const static size_t uid = 0x20;
    constexpr const static size_t itemList = 0x50;
    constexpr const static size_t itemCount = 0x18;
    constexpr const static size_t flags = 0x38;

// Functions
    constexpr const static size_t GetSlot = 0x349f6e0;
}

#define PlayerLoot_TypeDefinitionIndex 1292

namespace PlayerLoot_Offsets {

// Offsets
    constexpr const static size_t containers = 0x38;
}

#define PlayerInventory_TypeDefinitionIndex 1828

namespace PlayerInventory_Offsets {

// Offsets
    constexpr const static size_t containerMain = 0x38;
    constexpr const static size_t containerBelt = 0x58;
    constexpr const static size_t containerWear = 0x60;
    constexpr const static size_t loot = 0x48;

// Functions
}

#define PlayerEyes_TypeDefinitionIndex 2543

namespace PlayerEyes_Offsets {

// Offsets
    constexpr const static size_t viewOffset = 0x40;
    constexpr const static size_t bodyRotation = 0x50;
    constexpr const static size_t headAngles = 0x60;

// Functions
    constexpr const static size_t get_position = 0x1ebcf30;
    constexpr const static size_t get_rotation = 0x1ef4d90;
    constexpr const static size_t set_rotation = 0x1ec3210;
    constexpr const static size_t HeadForward = 0x1ecab00;
}

// obf name: ::%9a3f7822321144ee9b85ef01dea2a676e22c396b
#define PlayerEyes_Static_ClassName "PlayerEyes/%9a3f7822321144ee9b85ef01dea2a676e22c396b"
#define PlayerEyes_Static_ClassNameShort "%9a3f7822321144ee9b85ef01dea2a676e22c396b"
#define PlayerEyes_Static_TypeDefinitionIndex 2544

namespace PlayerEyes_Static_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1166e2d0;

// Offsets
    constexpr const static size_t EyeOffset = 0x1c;
}

// obf name: ::%243aaed1daa41286a316365e43691594ecedb3c0
#define PlayerBelt_ClassName "%243aaed1daa41286a316365e43691594ecedb3c0"
#define PlayerBelt_ClassNameShort "%243aaed1daa41286a316365e43691594ecedb3c0"
#define PlayerBelt_TypeDefinitionIndex 3989

namespace PlayerBelt_Offsets {

// Functions
}

// obf name: ::%ec927ed64bd1bf91135137433fb8ecd3c82b81eb
#define LocalPlayer_ClassName "%ec927ed64bd1bf91135137433fb8ecd3c82b81eb"
#define LocalPlayer_ClassNameShort "%ec927ed64bd1bf91135137433fb8ecd3c82b81eb"
#define LocalPlayer_TypeDefinitionIndex 3903

namespace LocalPlayer_Offsets {

// Functions
    constexpr const static size_t ItemCommand = 0x3132670;
    constexpr const static size_t get_Entity = 0x3124f40;
}

// obf name: ::%7a5542dba783ded56db1f303fcdb8e5044f58da1
#define LocalPlayer_Static_ClassName "%ec927ed64bd1bf91135137433fb8ecd3c82b81eb/%7a5542dba783ded56db1f303fcdb8e5044f58da1"
#define LocalPlayer_Static_ClassNameShort "%7a5542dba783ded56db1f303fcdb8e5044f58da1"
#define LocalPlayer_Static_TypeDefinitionIndex 3906

namespace LocalPlayer_Static_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11587700;

// Offsets
    constexpr const static size_t Entity = 0x8;
}

// obf name: ::%e109739fef12dea565f7003052f887533811550d
#define BasePlayer_Static_ClassName "BasePlayer/%e109739fef12dea565f7003052f887533811550d"
#define BasePlayer_Static_ClassNameShort "%e109739fef12dea565f7003052f887533811550d"
#define BasePlayer_Static_TypeDefinitionIndex 3261

namespace BasePlayer_Static_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115a21e8;

// Offsets
    constexpr const static size_t visiblePlayerList = 0x648;
}

#define BasePlayer_TypeDefinitionIndex 3237

namespace BasePlayer_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1159e8e8;

// Offsets
    constexpr const static size_t playerModel = 0x2e8;
    constexpr const static size_t input = 0x728;
    constexpr const static size_t movement = 0x510;
    constexpr const static size_t metabolism = 0x4b8;
    constexpr const static size_t blueprints = 0x490;
    constexpr const static size_t currentTeam = 0x550;
    constexpr const static size_t clActiveItem = 0x580;
    constexpr const static size_t modelState = 0x5c0;
    constexpr const static size_t playerFlags = 0x6d0;
    constexpr const static size_t eyes = 0x708;
    constexpr const static size_t playerRigidbody = 0x3f0;
    constexpr const static size_t userID = 0x718;
    constexpr const static size_t UserIDString = 0x698;
    constexpr const static size_t inventory = 0x308;
    constexpr const static size_t _displayName = 0x390;
    constexpr const static size_t _lookingAt = 0x6e8;
    constexpr const static size_t lastSentTickTime = 0x690;
    constexpr const static size_t lastSentTick = 0x628;
    constexpr const static size_t mounted = 0x5d8;
    constexpr const static size_t _lookingAtEntity = 0x320;
    constexpr const static size_t currentGesture = 0x3a0;
    constexpr const static size_t weaponMoveSpeedScale = 0x7b0;
    constexpr const static size_t clothingBlocksAiming = 0x7b4;
    constexpr const static size_t clothingMoveSpeedReduction = 0x7b8;
    constexpr const static size_t clothingWaterSpeedBonus = 0x7bc;
    constexpr const static size_t equippingBlocked = 0x7c4;

// Functions
    constexpr const static size_t IsOnGround = 0x26d4790;
    constexpr const static size_t GetSpeed = 0x28ca9c0;
    constexpr const static size_t GetMounted = 0x27d2370;
    constexpr const static size_t GetHeldEntity = 0x27bdf40;
    constexpr const static size_t get_inventory = 0x28e0680;
    constexpr const static size_t get_eyes = 0x27bc930;
    constexpr const static size_t OnAttacked = 0x2755a40;
    constexpr const static size_t OnAttacked_vtableoff = 0x39c8;
}

namespace modelstate_flags {
    inline constexpr std::uintptr_t Ducked = 0x1;
    inline constexpr std::uintptr_t Jumped = 0x2;
    inline constexpr std::uintptr_t OnGround = 0x4;
    inline constexpr std::uintptr_t Sleeping = 0x8;
    inline constexpr std::uintptr_t Sprinting = 0x10;
    inline constexpr std::uintptr_t OnLadder = 0x20;
    inline constexpr std::uintptr_t Flying = 0x40;
    inline constexpr std::uintptr_t Aiming = 0x80;
    inline constexpr std::uintptr_t Prone = 0x100;
    inline constexpr std::uintptr_t Mounted = 0x200;
    inline constexpr std::uintptr_t Relaxed = 0x400;
    inline constexpr std::uintptr_t Unused = 0x800;
    inline constexpr std::uintptr_t Crawling = 0x1000;
    inline constexpr std::uintptr_t Loading = 0x2000;
    inline constexpr std::uintptr_t HeadLook = 0x4000;
    inline constexpr std::uintptr_t HasParachute = 0x8000;
    inline constexpr std::uintptr_t Blocking = 0x10000;
    inline constexpr std::uintptr_t Ragdolling = 0x20000;
    inline constexpr std::uintptr_t Catching = 0x40000;
}

namespace base_player_flags {
    inline constexpr std::uintptr_t Unused1 = 0x1;
    inline constexpr std::uintptr_t CombatZone = 0x2;
    inline constexpr std::uintptr_t IsAdmin = 0x4;
    inline constexpr std::uintptr_t ReceivingSnapshot = 0x8;
    inline constexpr std::uintptr_t Sleeping = 0x10;
    inline constexpr std::uintptr_t Spectating = 0x20;
    inline constexpr std::uintptr_t Wounded = 0x40;
    inline constexpr std::uintptr_t IsDeveloper = 0x80;
    inline constexpr std::uintptr_t Connected = 0x100;
    inline constexpr std::uintptr_t ThirdPersonViewmode = 0x400;
    inline constexpr std::uintptr_t EyesViewmode = 0x800;
    inline constexpr std::uintptr_t ChatMute = 0x1000;
    inline constexpr std::uintptr_t NoSprint = 0x2000;
    inline constexpr std::uintptr_t Aiming = 0x4000;
    inline constexpr std::uintptr_t DisplaySash = 0x8000;
    inline constexpr std::uintptr_t Relaxed = 0x10000;
    inline constexpr std::uintptr_t SafeZone = 0x20000;
    inline constexpr std::uintptr_t ServerFall = 0x40000;
    inline constexpr std::uintptr_t Incapacitated = 0x80000;
    inline constexpr std::uintptr_t Workbench1 = 0x100000;
    inline constexpr std::uintptr_t Workbench2 = 0x200000;
    inline constexpr std::uintptr_t Workbench3 = 0x400000;
    inline constexpr std::uintptr_t VoiceRangeBoost = 0x800000;
    inline constexpr std::uintptr_t ModifyClan = 0x1000000;
    inline constexpr std::uintptr_t LoadingAfterTransfer = 0x2000000;
    inline constexpr std::uintptr_t NoRespawnZone = 0x4000000;
    inline constexpr std::uintptr_t IsInTutorial = 0x8000000;
    inline constexpr std::uintptr_t IsRestrained = 0x10000000;
    inline constexpr std::uintptr_t CreativeMode = 0x20000000;
    inline constexpr std::uintptr_t WaitingForGestureInteraction = 0x40000000;
    inline constexpr std::uintptr_t Ragdolling = 0x80000000;
}

namespace BaseEntityFlags {
    inline constexpr std::uintptr_t Placeholder = 0x1;
    inline constexpr std::uintptr_t On = 0x2;
    inline constexpr std::uintptr_t OnFire = 0x4;
    inline constexpr std::uintptr_t Open = 0x8;
    inline constexpr std::uintptr_t Locked = 0x10;
    inline constexpr std::uintptr_t Debugging = 0x20;
    inline constexpr std::uintptr_t Disabled = 0x40;
    inline constexpr std::uintptr_t Reserved1 = 0x80;
    inline constexpr std::uintptr_t Reserved2 = 0x100;
    inline constexpr std::uintptr_t Reserved3 = 0x200;
    inline constexpr std::uintptr_t Reserved4 = 0x400;
    inline constexpr std::uintptr_t Reserved5 = 0x800;
    inline constexpr std::uintptr_t Broken = 0x1000;
    inline constexpr std::uintptr_t Busy = 0x2000;
    inline constexpr std::uintptr_t Reserved6 = 0x4000;
    inline constexpr std::uintptr_t Reserved7 = 0x8000;
    inline constexpr std::uintptr_t Reserved8 = 0x10000;
    inline constexpr std::uintptr_t Reserved9 = 0x20000;
    inline constexpr std::uintptr_t Reserved10 = 0x40000;
    inline constexpr std::uintptr_t Reserved11 = 0x80000;
    inline constexpr std::uintptr_t InUse = 0x100000;
    inline constexpr std::uintptr_t Reserved12 = 0x200000;
    inline constexpr std::uintptr_t Reserved13 = 0x400000;
    inline constexpr std::uintptr_t Unused23 = 0x800000;
    inline constexpr std::uintptr_t Protected = 0x1000000;
    inline constexpr std::uintptr_t Transferring = 0x2000000;
    inline constexpr std::uintptr_t Reserved14 = 0x4000000;
    inline constexpr std::uintptr_t Reserved15 = 0x8000000;
    inline constexpr std::uintptr_t Reserved16 = 0x10000000;
    inline constexpr std::uintptr_t Reserved17 = 0x20000000;
    inline constexpr std::uintptr_t Reserved18 = 0x40000000;
    inline constexpr std::uintptr_t Reserved19 = 0x80000000;
}

#define BaseMovement_TypeDefinitionIndex 1753

namespace BaseMovement_Offsets {

// Offsets
    constexpr const static size_t adminCheat = 0x20;
    constexpr const static size_t Owner = 0x30;
    constexpr const static size_t velocityVec = 0x38;
    constexpr const static size_t targetMovement = 0x44;
    constexpr const static size_t sprintAmount = 0x50;
    constexpr const static size_t speedScale = 0x5c;
}

#define BuildingPrivlidge_TypeDefinitionIndex 1124

namespace BuildingPrivlidge_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115bf700;

// Offsets
    constexpr const static size_t allowedConstructionItems = 0x408;
    constexpr const static size_t cachedProtectedMinutes = 0x410;
}

#define WorldItem_TypeDefinitionIndex 4779

namespace WorldItem_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115ddcf8;

// Offsets
    constexpr const static size_t allowPickup = 0x200;
    constexpr const static size_t item = 0x208;
}

#define MainCamera_TypeDefinitionIndex 5184

namespace MainCamera_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115bb318;

// Offsets
    constexpr const static size_t mainCamera = 0x30;
    constexpr const static size_t mainCameraTransform = 0x28;
    constexpr const static size_t cullingMask = 0x42c;

// Functions
    constexpr const static size_t Update = 0x3f93ce0;
    constexpr const static size_t OnPreCull = 0x3f93170;
    constexpr const static size_t Trace = 0x3f8d350;
}

#define Item_TypeDefinitionIndex 3858

// obf name: ::%4576a9b3e0e5578c465eb60bbbde56d3bde1e98f
namespace Item_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1162c928;

// Offsets
    constexpr const static size_t info = 0xc0;
    constexpr const static size_t uid = 0xd8;
    constexpr const static size_t clientAmmoCount = 0x70;
    constexpr const static size_t heldEntity = 0x88;
    constexpr const static size_t amount = 0xf4;
    constexpr const static size_t position = 0x98;
    constexpr const static size_t _condition = 0x40;
    constexpr const static size_t _maxCondition = 0xa0;
}

#define OreResourceEntity_TypeDefinitionIndex 1069

namespace OreResourceEntity_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115dea50;
}

#define CollectibleEntity_TypeDefinitionIndex 7206

namespace CollectibleEntity_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115dde18;
}

#define DroppedItemContainer_TypeDefinitionIndex 4968

namespace DroppedItemContainer_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115dea10;
    constexpr const static size_t playerSteamID = 0x2e8;
    constexpr const static size_t _playerName = 0x2d0;
}

#define TOD_Sky_TypeDefinitionIndex 1220

namespace TOD_Sky_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11612ba0;
    constexpr const static size_t Cycle = 0x40;
    constexpr const static size_t Atmosphere = 0x50;
    constexpr const static size_t Day = 0x58;
    constexpr const static size_t Night = 0x60;
    constexpr const static size_t Stars = 0x78;
    constexpr const static size_t Clouds = 0x80;
    constexpr const static size_t Ambient = 0x98;
}

#define TOD_Sky_Static_TypeDefinitionIndex 1222

namespace TOD_Sky_Static_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x1165dc98;
    constexpr const static size_t static_fields = 0xb8;
    constexpr const static size_t instances = 0x20;
}

#define ConsoleSystem_Command_TypeDefinitionIndex 17

namespace ConsoleSystem_Command_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x11655fe0;
    constexpr const static size_t GetOveride = 0x70;
    constexpr const static size_t SetOveride = 0x20;
    constexpr const static size_t Call = 0x10;
}

#define HiddenValue_TypeDefinitionIndex 1639

namespace HiddenValue_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x117f8710;
    constexpr const static size_t _handle = 0x18;
    constexpr const static size_t _accessCount = 0x10;
    constexpr const static size_t _hasValue = 0x14;
}

#define BaseVehicle_TypeDefinitionIndex 2728

namespace BaseVehicle_Offsets {
    inline constexpr std::uintptr_t typeinfo = 0x115e81e0;
    constexpr const static size_t mountPoints = 0x3f0;
    constexpr const static size_t ignoreDamageFromOutside = 0x3e4;
}

// NOTE: this local copy trims the very long tail of unrelated UI / NPC /
// building / vehicle / water / map / audio class blocks from the original
// paste (ScientistNPC, TunnelDweller, MapView, Planner, StorageContainer,
// LootContainer, TOD_* parameter structs, ItemIcon, ScrollRect, dozens more)
// since none of them are consumed by OFF in legacy_runtime.py today. If a
// future feature needs one of those, re-paste the original message from
// Discord (2026-08-20) rather than guessing — it had them all in full.
