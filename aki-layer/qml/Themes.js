.pragma library

var themes = {
    "Aki": {
        name: "Aki",
        tag: "秋",
        avatar: "qrc:/avatars/aki.jpg",
        background: "qrc:/qml/AutumnLeaves.qml",
        gradientTop: "#FFFFF8FB",
        gradientMid: "#FFFBE4EC",
        gradientBottom: "#FFF4C9D9",
        border: "#78F3A9C0",
        borderHover: "#C9F07FA6",
        accent: "#A53A61",
        text: "#4B2A37",
        tagBg: "#28B43D6C",
        tagText: "#B43D6C"
    },
    "Fuyu": {
        name: "Fuyu",
        tag: "冬",
        avatar: "qrc:/avatars/fuyu.jpg",
        background: "qrc:/qml/SnowflakeSilhouette.qml",
        gradientTop: "#FFF6FCFF",
        gradientMid: "#FFE4F4FF",
        gradientBottom: "#FFCCE8FA",
        border: "#7899CCE5",
        borderHover: "#C97EBFDF",
        accent: "#2E6F96",
        text: "#294653",
        tagBg: "#2899CCE8",
        tagText: "#2E6F96"
    },
    "Haru": {
        name: "Haru",
        tag: "春",
        avatar: "qrc:/avatars/haru.jpg",
        background: "qrc:/qml/CreativeSilhouette.qml",
        gradientTop: "#FFF7FFFA",
        gradientMid: "#FFE0F7E8",
        gradientBottom: "#FFC4EED4",
        border: "#7890D8AE",
        borderHover: "#C96FB98B",
        accent: "#2D7C53",
        text: "#244535",
        tagBg: "#2897D9B2",
        tagText: "#2D7C53"
    },
    "Natsu": {
        name: "Natsu",
        tag: "夏",
        avatar: "qrc:/avatars/natsu.jpg",
        background: "qrc:/qml/GeometrySilhouette.qml",
        gradientTop: "#FFFFF8F7",
        gradientMid: "#FFFBE2DF",
        gradientBottom: "#FFF6C8C3",
        border: "#78DF9A92",
        borderHover: "#C9D97E76",
        accent: "#9E3E39",
        text: "#472A28",
        tagBg: "#28D97A70",
        tagText: "#9E3E39"
    }
}

function normalize(value) {
    var key = String(value || "").trim().toLowerCase()
    if (key === "fuyu" || key === "冬")
        return "Fuyu"
    if (key === "haru" || key === "春")
        return "Haru"
    if (key === "natsu" || key === "夏")
        return "Natsu"
    return "Aki"
}

function get(value) {
    return themes[normalize(value)]
}
