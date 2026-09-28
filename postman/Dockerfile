FROM docker.io/postman/newman:alpine
WORKDIR /etc/newman
COPY rpg_api_collection.json .
ENTRYPOINT ["newman", "run", "rpg_api_collection.json"]
