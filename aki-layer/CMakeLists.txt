cmake_minimum_required(VERSION 3.21)

project(aki-layer VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Qml Quick QuickControls2 DBus)
find_package(LayerShellQt REQUIRED)

qt_standard_project_setup(REQUIRES 6.5)

qt_add_executable(aki-layer
    src/main.cpp
    src/bubblecontrolserver.cpp
    src/bubblecontrolserver.h
    src/inputregioncontroller.cpp
    src/inputregioncontroller.h
    src/notificationhelper.cpp
    src/notificationhelper.h
)

qt_add_resources(aki-layer aki-layer-qml
    PREFIX "/"
    FILES
        qml/Main.qml
        qml/PresencePanel.qml
        qml/BubbleStack.qml
        qml/NotificationBubble.qml
        qml/AutumnLeaves.qml
        qml/SnowflakeSilhouette.qml
        qml/CreativeSilhouette.qml
        qml/GeometrySilhouette.qml
        qml/Themes.js
)

get_filename_component(AKI_REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
qt_add_resources(aki-layer aki-layer-avatars
    PREFIX "/avatars"
    BASE "${AKI_REPO_ROOT}/assets/small"
    FILES
        "${AKI_REPO_ROOT}/assets/small/aki.jpg"
        "${AKI_REPO_ROOT}/assets/small/fuyu.jpg"
        "${AKI_REPO_ROOT}/assets/small/haru.jpg"
        "${AKI_REPO_ROOT}/assets/small/natsu.jpg"
)

target_link_libraries(aki-layer PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Qml
    Qt6::Quick
    Qt6::QuickControls2
    Qt6::DBus
    LayerShellQt::Interface
)

install(TARGETS aki-layer RUNTIME DESTINATION bin)
