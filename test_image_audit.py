from image_audit import extract_images

def test_extract_images():
    data = {"Image": {
        "HighPic": "https://images.icecat.biz/img/gallery/x.jpg",
        "HighPicWidth": "640", "HighPicHeight": "640",
        "LowPic": "https://media.ipcstore.net/img/l/x.jpg",
        "Pic500x500": "https://images.icecat.biz/img/gallery_mediums/x.jpg",
    }, "Multimedia": [{"Pic": "https://example.com/extra.jpg"}]}
    main_url, all_imgs = extract_images(data)
    assert main_url == "https://images.icecat.biz/img/gallery_mediums/x.jpg", main_url
    assert all_imgs[0] == main_url
    assert extract_images(None) == ("", [])
    assert extract_images({})[0] == ""


if __name__ == "__main__":
    test_extract_images()
    print("ok")
